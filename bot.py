import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import asyncio
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="INFO")
from dotenv import load_dotenv
import uvloop
uvloop.install()

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import LLMRunFrame, EndFrame, CancelTaskFrame, EndTaskFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, TranscriptionFrame, FunctionCallResultProperties, TextFrame, AudioRawFrame, LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.audio.schedulers.fixed_size_scheduler import FixedSizeScheduler
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService, GeminiVADParams
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer

load_dotenv(override=True)

import sync_knowledgebase
import civicrm_lookup
import civicrm_agent
import zammad_cti

app = FastAPI()

# Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# Note: Using the model name without the "models/" prefix or v1alpha features
# was required to resolve the 1011 internal error.
MODEL_NAME = "gemini-3.1-flash-live-preview"

if not GOOGLE_API_KEY:
    logger.error("GOOGLE_API_KEY not found in environment variables")
    sys.exit(1)

STUDIO_WEBHOOK_URL = os.getenv("STUDIO_WEBHOOK_URL")
if not STUDIO_WEBHOOK_URL:
    logger.warning("STUDIO_WEBHOOK_URL not found in environment. Call continuation will be disabled.")

# Global state for call transfers and hangups
pending_transfers = {}
pending_hangups = set()
active_calls = {} # {call_sid: {"from": ..., "to": ...}}

# Sync knowledgebase on startup
logger.info("Syncing knowledgebase from Zammad...")
try:
    sync_knowledgebase.main()
except Exception as e:
    logger.error(f"Failed to sync knowledgebase: {e}")

# Load system prompt from file and append knowledgebase
SYSTEM_PROMPT_PATH = Path(__file__).parent / "SYSTEM_PROMPT.md"
KNOWLEDGEBASE_DIR = Path(__file__).parent / "knowledgebase"

try:
    with open(SYSTEM_PROMPT_PATH, "r") as f:
        SYSTEM_PROMPT = f.read()
    
    if KNOWLEDGEBASE_DIR.exists():
        kb_content = "\n\n# KNOWLEDGE BASE\n"
        for md_file in KNOWLEDGEBASE_DIR.glob("**/*.md"):
            with open(md_file, "r") as f:
                kb_content += f"\n\n## {md_file.name}\n"
                kb_content += f.read()
        SYSTEM_PROMPT += kb_content
        
    logger.info(f"Loaded system prompt and knowledgebase. Total length: {len(SYSTEM_PROMPT)}")
except Exception as e:
    logger.error(f"Failed to load system prompt or knowledgebase: {e}")
    sys.exit(1)

@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    host = request.url.netloc
    
    if request.method == "POST":
        data = await request.form()
    else:
        data = request.query_params
        
    from_number = data.get("From", "Unknown Caller")
    to_number = data.get("To", "Unknown")
    caller_name = data.get("CallerName", "")
        
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://{host}/ws">
                <Parameter name="caller_number" value="{from_number}" />
                <Parameter name="destination_number" value="{to_number}" />
                <Parameter name="caller_name" value="{caller_name}" />
            </Stream>
        </Connect>
        <Redirect method="POST">https://{host}/post_bot</Redirect>
    </Response>"""
    return Response(content=twiml_response, media_type="application/xml")

@app.post("/post_bot")
async def post_bot(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    
    if call_sid in pending_hangups:
        logger.info(f"Executing hangup for {call_sid}")
        pending_hangups.remove(call_sid)
        return Response(content='<Response><Hangup/></Response>', media_type="application/xml")
    
    transfer_data = pending_transfers.pop(call_sid, None)
    if transfer_data:
        target_number = transfer_data["number"]
        target_name = transfer_data["name"]
        
        # Get caller info to send the 'answer' event to Zammad
        call_info = active_calls.get(call_sid, {})
        from_num = call_info.get("from", "Unknown")
        
        logger.info(f"Executing transfer for {call_sid} to {target_name} ({target_number})")
        
        # Push 'answer' to Zammad with the new target person's name
        asyncio.create_task(zammad_cti.push_cti_event(
            event="answer",
            from_number=from_num,
            to_number=target_number,
            direction="in",
            call_id=call_sid,
            user_name=target_name
        ))

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Dial>{target_number}</Dial>
        </Response>"""
        return Response(content=twiml, media_type="application/xml")
    
    # Cleanup active_calls
    active_calls.pop(call_sid, None)
    
    if STUDIO_WEBHOOK_URL:
        sep = "&" if "?" in STUDIO_WEBHOOK_URL else "?"
        studio_return_url = f"{STUDIO_WEBHOOK_URL}{sep}FlowEvent=return"
        logger.info(f"Returning call {call_sid} to Studio Flow")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect method="POST">{studio_return_url}</Redirect>
        </Response>"""
        return Response(content=twiml, media_type="application/xml")
    
    return Response(content='<Response><Hangup/></Response>', media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio WebSocket connection accepted")

    transport_type, call_data = await parse_telephony_websocket(websocket)
    call_sid = call_data["call_id"]
    
    # Set up per-call logging
    log_file = f"logs/call_{call_sid}.log"
    handler_id = logger.add(
        log_file, 
        filter=lambda record: record["extra"].get("call_id") == call_sid,
        format="{time} | {level: <8} | {message}",
        level="DEBUG" # Keep debug on for call files to catch jitter
    )
    call_logger = logger.bind(call_id=call_sid)
    call_logger.info(f"Accepted {transport_type} call: {call_data}")
    
    caller_number = call_data.get("body", {}).get("caller_number", "Unknown Caller")
    destination_number = call_data.get("body", {}).get("destination_number", "Unknown")
    caller_name = call_data.get("body", {}).get("caller_name", "")

    # Store active call info for CTI updates during transfer
    active_calls[call_sid] = {
        "from": caller_number,
        "to": destination_number
    }

    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        params=TwilioFrameSerializer.InputParams(
            auto_hang_up=False
        )
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
            fixed_audio_packet_size=320,
            audio_out_can_send_silence=False,
            audio_out_scheduler=FixedSizeScheduler(chunk_size=320)
        )
    )

    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="end_call",
                description="Ends the phone call. CRITICAL: You MUST call this tool immediately in the exact same turn after you say goodbye or conclude the conversation. Do not wait for the user to hang up first.",
                properties={},
                required=[]
            ),
            FunctionSchema(
                name="report_missing_knowledge",
                description="Report a gap in the knowledge base. Use this silently when you cannot find a specific answer or when the existing docs are insufficient. Describe who is calling, what they specifically want to know, and why the current documentation didn't help.",
                properties={
                    "observation": {
                        "type": "string",
                        "description": "Detailed description of the knowledge gap, including caller context and why the documentation was insufficient."
                    }
                },
                required=["observation"]
            ),
            FunctionSchema(
                name="transfer_call",
                description="Transfer the current call to another phone number. Use this when the user asks to speak to a person or a specific volunteer.",
                properties={
                    "phone_number": {
                        "type": "string",
                        "description": "The E.164 formatted phone number to transfer to (e.g. +12105551212)."
                    },
                    "contact_name": {
                        "type": "string",
                        "description": "The name of the person being transferred to (e.g. Greg)."
                    }
                },
                required=["phone_number", "contact_name"]
            ),
            FunctionSchema(
                name="lookup_contact",
                description="Look up a contact name in the CiviCRM database to get their phone number. This does NOT transfer the call. You must use transfer_call with the resulting phone number to actually transfer.",
                properties={
                    "contact_name": {
                        "type": "string",
                        "description": "The full name of the contact to look up (e.g. Greg Thibodeaux)."
                    }
                },
                required=["contact_name"]
            ),
            FunctionSchema(
                name="check_my_membership",
                description="Checks the caller's current membership status, type, and expiration date. Only use this if the caller is recognized.",
                properties={},
                required=[]
            ),
            FunctionSchema(
                name="list_my_contact_info",
                description="Lists all addresses, phone numbers, and email addresses we have on file for the caller. Only use this if the caller is recognized.",
                properties={},
                required=[]
            ),
            FunctionSchema(
                name="create_my_contact_record",
                description="Creates a new contact record for the current caller in our database. Use this IMMEDIATELY after an unrecognized caller has provided their first and last name, so that we can accurately log their inquiry.",
                properties={
                    "first_name": {"type": "string", "description": "The caller's first name."},
                    "last_name": {"type": "string", "description": "The caller's last name."}
                },
                required=["first_name", "last_name"]
            ),
            FunctionSchema(
                name="add_new_address",
                description="Adds a new address to the caller's record. Does NOT delete old ones.",
                properties={
                    "street_address": {"type": "string", "description": "Street address (e.g. 123 Main St)."},
                    "city": {"type": "string", "description": "City (e.g. San Antonio)."},
                    "postal_code": {"type": "string", "description": "5-digit zip code (e.g. 78201)."},
                    "is_primary": {"type": "boolean", "description": "Whether this should be the primary address."}
                },
                required=["street_address", "city", "postal_code"]
            ),
            FunctionSchema(
                name="add_new_phone",
                description="Adds a new phone number to the caller's record.",
                properties={
                    "phone_number": {"type": "string", "description": "Phone number in E.164 format (e.g. +12105551212)."},
                    "is_primary": {"type": "boolean", "description": "Whether this should be the primary phone number."}
                },
                required=["phone_number"]
            ),
            FunctionSchema(
                name="add_new_email",
                description="Adds a new email address to the caller's record.",
                properties={
                    "email_address": {"type": "string", "description": "Email address (e.g. user@example.com)."},
                    "is_primary": {"type": "boolean", "description": "Whether this should be the primary email."}
                },
                required=["email_address"]
            ),
            FunctionSchema(
                name="set_info_as_primary",
                description="Changes which existing record is marked as primary. Requires the Record ID (provided by list_my_contact_info) and the entity type.",
                properties={
                    "entity_type": {
                        "type": "string",
                        "enum": ["Address", "Phone", "Email"],
                        "description": "The type of record being updated."
                    },
                    "record_id": {
                        "type": "integer",
                        "description": "The unique ID of the specific record to make primary."
                    }
                },
                required=["entity_type", "record_id"]
            )
        ],
        custom_tools={AdapterType.GEMINI: [{"google_search": {}}]},
    )

    llm = GeminiLiveLLMService(
        api_key=GOOGLE_API_KEY,
        settings=GeminiLiveLLMService.Settings(
            model=MODEL_NAME,
            system_instruction=SYSTEM_PROMPT,
            voice="Charon",
            vad=GeminiVADParams(
                start_sensitivity="START_SENSITIVITY_LOW",
                end_of_speech_sensitivity="START_SENSITIVITY_LOW"
            )
        ),
        tools=tools,
        reconnect_on_error=False
    )

    class SpeechTracker(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.is_speaking = False

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await self.push_frame(frame, direction)
            if isinstance(frame, BotStartedSpeakingFrame):
                self.is_speaking = True
                call_logger.debug("Bot started speaking")
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.is_speaking = False
                call_logger.debug("Bot stopped speaking")
            elif isinstance(frame, TranscriptionFrame):
                role = "User" if frame.user_id == "user" else "Bot"
                call_history.append(f"[{role}] {frame.text}")
            elif isinstance(frame, LLMContextFrame):
                for msg in reversed(frame.context.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        curr_text = msg["content"]
                        if not call_history or call_history[-1] != f"[Bot] {curr_text}":
                            call_history.append(f"[Bot] {curr_text}")
                        break

    speech_tracker = SpeechTracker()

    async def await_bot_silence(timeout=4.0):
        """Waits for the bot to stop speaking, with a timeout to avoid pipeline hangs."""
        start = asyncio.get_event_loop().time()
        while speech_tracker.is_speaking and (asyncio.get_event_loop().time() - start) < timeout:
            await asyncio.sleep(0.1)

    async def wait_and_terminate():
        call_logger.info("wait_and_terminate started: waiting for audio to finish.")
        await asyncio.sleep(1.0)
        max_wait = 30.0
        start_time = asyncio.get_event_loop().time()
        while speech_tracker.is_speaking and (asyncio.get_event_loop().time() - start_time) < max_wait:
            await asyncio.sleep(0.1)
        call_logger.info("Bot finished speaking. Waiting 0.5s for network flush...")
        await asyncio.sleep(0.5)
        call_logger.info("Terminating pipeline with CancelTaskFrame.")
        await llm.push_frame(CancelTaskFrame(), FrameDirection.UPSTREAM)

    async def hang_up(params: FunctionCallParams):
        call_sid = call_data["call_id"]
        call_logger.info(f"Bot is ending the call {call_sid} via end_call tool")
        pending_hangups.add(call_sid)
        asyncio.create_task(wait_and_terminate())

    async def notify_slack(params: FunctionCallParams):
        observation = params.arguments.get("observation")
        webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            call_logger.error("SLACK_WEBHOOK_URL not found in environment")
            await params.result_callback({"status": "error"})
            return
        
        payload = {"message": f"Knowledge Base Gap Reported:\n{observation}"}
        # Fire and forget the network call
        asyncio.create_task(httpx.AsyncClient(timeout=4.5).post(webhook_url, json=payload))
        call_logger.info(f"Notified Slack about missing knowledge: {observation[:50]}...")

        # Hybrid Speech-Aware Logic:
        # If the bot is already speaking (Parallel mode), wait for it to finish 
        # before returning the result to prevent a speculative turn restart.
        # Use an instructional result to steer the model away from "goodbyes".
        was_speaking = speech_tracker.is_speaking
        if was_speaking:
            await await_bot_silence()
        
        await params.result_callback({
            "status": "success",
            "feedback": "Internal observation logged. Instruction: DO NOT mention this log to the caller. Proceed with your current response naturally."
        })

    async def transfer_call_handler(args: FunctionCallParams):
        phone_number = args.arguments.get("phone_number")
        contact_name = args.arguments.get("contact_name", "a volunteer")
        call_logger.info(f"Transferring call for {call_data['call_id']} to {contact_name} at {phone_number}")
        
        pending_transfers[call_data["call_id"]] = {
            "number": phone_number,
            "name": contact_name
        }
        await task.queue_frames([EndFrame()])
        return {"status": "transfer_initiated"}

    async def lookup_contact_handler(params: FunctionCallParams):
        contact_name = params.arguments.get("contact_name")
        call_logger.info(f"Bot requesting CiviCRM lookup for: {contact_name}")
        try:
            contacts = await asyncio.wait_for(civicrm_lookup.lookup_contact_by_name(contact_name), timeout=4.5)
            if len(contacts) == 1 and len(contacts[0]["phones"]) == 1:
                contact = contacts[0]
                phone_number = contact["phones"][0]["number"]
                logger.info(f"Unique match found for {contact_name}: {phone_number}.")
                await params.result_callback({"status": "success", "phone_number": phone_number, "message": f"Found {contact_name}."})
                return
            error_msg = civicrm_lookup.format_disambiguation_message(contacts)
            await params.result_callback({"status": "error", "message": error_msg})
        except asyncio.TimeoutError:
            await params.result_callback({"status": "error", "message": "Lookup timed out."})
        except Exception as e:
            await params.result_callback({"status": "error", "message": str(e)})

    async def get_membership_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "I don't recognize your phone number."})
            return
        info = await civicrm_agent.get_membership_info(caller_contact_id)
        await params.result_callback({"status": "success", "message": info})

    async def list_info_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        summary = await civicrm_agent.list_contact_info(caller_contact_id)
        await params.result_callback({"status": "success", "message": summary})

    async def add_address_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.add_address(caller_contact_id, params.arguments.get("street_address"), params.arguments.get("city"), params.arguments.get("postal_code"), params.arguments.get("is_primary", False))
        await params.result_callback({"status": "success", "message": result})

    async def add_phone_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.add_phone(caller_contact_id, params.arguments.get("phone_number"), params.arguments.get("is_primary", False))
        await params.result_callback({"status": "success", "message": result})

    async def add_email_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.add_email(caller_contact_id, params.arguments.get("email_address"), params.arguments.get("is_primary", False))
        await params.result_callback({"status": "success", "message": result})

    async def set_primary_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.set_primary_record(params.arguments.get("entity_type"), params.arguments.get("record_id"))
        await params.result_callback({"status": "success", "message": result})

    async def create_contact_handler(params: FunctionCallParams):
        first = params.arguments.get("first_name")
        last = params.arguments.get("last_name")
        logger.info(f"Bot creating new contact: {first} {last} for number {caller_number}")
        result_data = await civicrm_agent.create_contact(first, last, caller_number)
        if result_data["success"]:
            nonlocal caller_contact_id
            caller_contact_id = result_data["contact_id"]
            await params.result_callback({"status": "success", "message": result_data["message"]})
        else:
            await params.result_callback({"status": "error", "message": result_data["message"]})

    llm.register_function("end_call", hang_up, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("report_missing_knowledge", notify_slack, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("transfer_call", transfer_call_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("lookup_contact", lookup_contact_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("check_my_membership", get_membership_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("list_my_contact_info", list_info_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("add_new_address", add_address_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("add_new_phone", add_phone_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("add_new_email", add_email_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("set_info_as_primary", set_primary_handler, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("create_my_contact_record", create_contact_handler, cancel_on_interruption=False, timeout_secs=5.0)

    async def session_warning_task():
        try:
            await asyncio.sleep(420)
            context.add_message({"role": "developer", "content": "SYSTEM WARNING: There are 3 minutes remaining in this call."})
            await asyncio.sleep(60)
            context.add_message({"role": "developer", "content": "SYSTEM WARNING: There are 2 minutes remaining. You must wrap up now."})
            await asyncio.sleep(60)
            context.add_message({"role": "developer", "content": "CRITICAL SYSTEM WARNING: There is only 1 minute remaining. You MUST end the call IMMEDIATELY."})
            await task.queue_frames([LLMRunFrame()])
        except asyncio.CancelledError:
            pass

    warning_task = asyncio.create_task(session_warning_task())

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        assistant_aggregator,
        speech_tracker,
        transport.output()
    ])

    task = PipelineTask(pipeline, params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000, enable_metrics=True, enable_usage_metrics=True))

    caller_contact_id = None

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected: {client}")
        now = datetime.now(ZoneInfo("America/Chicago")).strftime("%A, %B %d, %Y at %I:%M %p")
        
        nonlocal caller_contact_id
        contact_info = await civicrm_lookup.lookup_contact_by_phone(caller_number)
        
        detail_block = f"CURRENT CALLER INFO: Unknown Caller."
        if caller_name:
            detail_block = f"CURRENT CALLER INFO: Unknown but identified via CNAM as {caller_name}."
            greeting = f"'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Am I speaking with {caller_name}?'"
        else:
            greeting = "'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Who am I speaking with today?'"
        
        if contact_info:
            caller_contact_id = contact_info["contact_id"]
            name = contact_info["name"]
            
            # Fetch full profile for the prompt
            membership = await civicrm_agent.get_membership_info(caller_contact_id)
            contact_details = await civicrm_agent.list_contact_info(caller_contact_id)
            
            detail_block = f"CURRENT CALLER INFO: Recognized as {name} (ID: {caller_contact_id}).\n\n{membership}\n\n{contact_details}"
            greeting = f"'Hi {name}! Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! How can I help you today?'"
            
        # Determine the recipient label for Zammad CTI
        recipient_label = "Makerspace"
        if "2105470221" in destination_number:
            recipient_label = "Virtual Receptionist"
        elif "8559042954" in destination_number:
            recipient_label = "Test Line"

        # Trigger Zammad CTI answer event
        asyncio.create_task(zammad_cti.push_cti_event(
            "answer", 
            caller_number, 
            destination_number, 
            "in", 
            call_data["call_id"], 
            user_name=recipient_label,
            answering_number="10Bot"
        ))

        context.add_message(
            {"role": "developer", "content": f"SYSTEM INFO: The current date and time is {now}. The caller's phone number is {caller_number}.\n\n{detail_block}\n\nSimply say: {greeting}"}
        )

    # Keep conversation history in memory and dump at the end to avoid blocking audio loop
    call_history = []

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        call_logger.info(f"Client disconnected for call {call_data['call_id']}")
        warning_task.cancel()
        
        # Dump full conversation history to the log at the end
        call_logger.debug("--- FINAL TRANSCRIPT ---")
        for entry in call_history:
            call_logger.debug(entry)
        call_logger.debug("--- END TRANSCRIPT ---")

        if caller_contact_id:
            transcript = ""
            for msg in context.messages:
                # Ensure we handle messages without content safely
                if msg.get("role") not in ["system", "developer"] and msg.get("content"):
                    transcript += f"**{msg['role'].capitalize()}**: {msg['content']}\n\n"
            if transcript:
                await civicrm_agent.log_call_activity(caller_contact_id, "Inbound Call via 10Bot", f"Call Transcript:\n\n{transcript}")
        
        # Clean up per-call log sink
        logger.remove(handler_id)
        await task.cancel()

    @llm.event_handler("on_error")
    async def on_llm_error(service, error):
        # Force a pipeline exit on fatal service errors to avoid silent hangs
        call_logger.error(f"LLM Service Error: {error}")
        await task.queue_frames([EndFrame()])

    with logger.contextualize(call_id=call_sid):
        runner = PipelineRunner(handle_sigint=True)
        try:
            await runner.run(task)
        except Exception as e:
            call_logger.error(f"Error running pipeline: {e}")
            # Ensure the call doesn't stay open in silence on crash
            await task.queue_frames([EndFrame()])
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
