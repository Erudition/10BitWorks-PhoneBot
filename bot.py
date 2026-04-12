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
from dotenv import load_dotenv

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import LLMRunFrame, EndFrame, CancelTaskFrame, EndTaskFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
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

app = FastAPI()

# Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
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

@app.get("/twiml")
async def twiml(request: Request):
    host = request.url.netloc
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://{host}/ws" />
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
    
    target_number = pending_transfers.pop(call_sid, None)
    if target_number:
        logger.info(f"Executing transfer for {call_sid} to {target_number}")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Dial>{target_number}</Dial>
        </Response>"""
        return Response(content=twiml, media_type="application/xml")
    
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
    logger.info(f"Accepted {transport_type} call: {call_data}")
    
    # Extract the caller number passed via TwiML stream parameters
    caller_number = call_data.get("body", {}).get("caller_number", "Unknown Caller")

    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
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
            fixed_audio_packet_size=320
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
                    }
                },
                required=["phone_number"]
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
        ),
        tools=tools,
        reconnect_on_error=False
    )

    # State for tracking speech via a FrameProcessor
    class SpeechTracker(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.is_speaking = False

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            # Intercept frames pushed UPSTREAM by the output transport
            if isinstance(frame, BotStartedSpeakingFrame):
                self.is_speaking = True
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.is_speaking = False
            await self.push_frame(frame, direction)

    speech_tracker = SpeechTracker()

    async def wait_and_terminate():
        logger.info("wait_and_terminate started: waiting for audio to finish.")
        # 1. Give the bot a moment to start speaking
        await asyncio.sleep(1.0)
        
        # 2. Wait for the bot to finish its sentence
        max_wait = 30.0
        start_time = asyncio.get_event_loop().time()
        while speech_tracker.is_speaking and (asyncio.get_event_loop().time() - start_time) < max_wait:
            await asyncio.sleep(0.1)
            
        # 3. Small buffer to allow the last few packets to cross the network to Twilio
        logger.info("Bot finished speaking. Waiting 0.5s for network flush...")
        await asyncio.sleep(0.5)
        
        logger.info("Terminating pipeline with CancelTaskFrame.")
        await llm.push_frame(CancelTaskFrame(), FrameDirection.UPSTREAM)

    async def hang_up(params: FunctionCallParams):
        call_sid = call_data["call_id"]
        logger.info(f"Bot is ending the call {call_sid} via end_call tool")
        pending_hangups.add(call_sid)
        await params.result_callback({"status": "hanging_up"})
        asyncio.create_task(wait_and_terminate())

    async def notify_slack(params: FunctionCallParams):
        observation = params.arguments.get("observation")
        webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            logger.error("SLACK_WEBHOOK_URL not found in environment")
            await params.result_callback({"status": "error", "message": "Slack webhook not configured"})
            return

        payload = {"message": f"Knowledge Base Gap Reported:\n{observation}"}
        try:
            async with httpx.AsyncClient(timeout=4.5) as client:
                response = await client.post(webhook_url, json=payload)
                response.raise_for_status()
            logger.info(f"Notified Slack about missing knowledge: {observation[:50]}...")
            await params.result_callback({"status": "success", "message": "Observation logged for review. Continue the conversation naturally."})
        except Exception as e:
            logger.error(f"Failed to notify Slack: {e}")
            await params.result_callback({"status": "error", "message": str(e)})

    async def start_transfer(params: FunctionCallParams):
        phone_number = params.arguments.get("phone_number")
        call_sid = call_data["call_id"]
        logger.info(f"Bot requesting transfer to {phone_number} for call {call_sid}")
        pending_transfers[call_sid] = phone_number
        # By NOT returning a result_callback, we freeze the Gemini model
        # so it cannot generate any new hallucinated audio while the final
        # "Transferring you now" audio finishes playing and the pipeline terminates.
        asyncio.create_task(wait_and_terminate())

    async def lookup_contact_handler(params: FunctionCallParams):
        contact_name = params.arguments.get("contact_name")
        logger.info(f"Bot requesting CiviCRM lookup for: {contact_name}")
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

    llm.register_function("end_call", hang_up, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("report_missing_knowledge", notify_slack, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("transfer_call", start_transfer, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("lookup_contact", lookup_contact_handler, cancel_on_interruption=False, timeout_secs=5.0)

    async def session_warning_task():
        try:
            # 7 minute warning (420 seconds)
            await asyncio.sleep(420)
            logger.info("Sending 3-minute session warning to bot context.")
            context.add_message(
                {"role": "developer", "content": "SYSTEM WARNING: There are 3 minutes remaining in this call due to a technical limit. Please begin to wrap up the conversation."}
            )

            # 8 minute warning (+60 seconds)
            await asyncio.sleep(60)
            logger.info("Sending 2-minute session warning to bot context.")
            context.add_message(
                {"role": "developer", "content": "SYSTEM WARNING: There are 2 minutes remaining. You must wrap up now."}
            )

            # 9 minute warning (+60 seconds)
            await asyncio.sleep(60)
            logger.info("Sending 1-minute session warning to bot context.")
            context.add_message(
                {"role": "developer", "content": "CRITICAL SYSTEM WARNING: There is only 1 minute remaining. You MUST conclude the conversation and call the end_call tool IMMEDIATELY."}
            )
            # We ONLY force an interruption for the absolute final warning
            await task.queue_frames([LLMRunFrame()])
            
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Failed to send session warning: {e}")

    warning_task = asyncio.create_task(session_warning_task())

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        speech_tracker,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        )
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected: {client}")
        now = datetime.now(ZoneInfo("America/Chicago")).strftime("%A, %B %d, %Y at %I:%M %p")
        
        caller_name = await civicrm_lookup.lookup_contact_by_phone(caller_number)
        
        if caller_name:
            greeting = f"'Hi {caller_name}! Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! How can I help you today?'"
        else:
            greeting = "'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Who am I speaking with today?'"
            
        context.add_message(
            {"role": "developer", "content": f"SYSTEM INFO: The current date and time is {now}. The caller's phone number is {caller_number}. Simply say: {greeting}"}
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Client disconnected: {client}")
        warning_task.cancel()
        await task.cancel()

    runner = PipelineRunner(handle_sigint=True)
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"Error running pipeline: {e}")
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
