import os
import sys
from datetime import datetime
from pathlib import Path
import asyncio
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
from dotenv import load_dotenv

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import LLMRunFrame, EndFrame, CancelTaskFrame, EndTaskFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection
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
    
    # Append knowledgebase files
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
    
    # Construct the redirect URL for returning to Studio Flow
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
    
    # Check if a transfer was requested by the bot
    target_number = pending_transfers.pop(call_sid, None)
    
    if target_number:
        logger.info(f"Executing transfer for {call_sid} to {target_number}")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Dial>{target_number}</Dial>
        </Response>"""
        return Response(content=twiml, media_type="application/xml")
    
    # Otherwise, return back to the Studio Flow if configured
    if STUDIO_WEBHOOK_URL:
        sep = "&" if "?" in STUDIO_WEBHOOK_URL else "?"
        studio_return_url = f"{STUDIO_WEBHOOK_URL}{sep}FlowEvent=return"
        logger.info(f"Returning call {call_sid} to Studio Flow")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect method="POST">{studio_return_url}</Redirect>
        </Response>"""
        return Response(content=twiml, media_type="application/xml")
    
    # Fallback to hangup if no Studio URL is set
    return Response(content='<Response><Hangup/></Response>', media_type="application/xml")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio WebSocket connection accepted")

    # Use Pipecat utility to parse the initial Twilio handshake
    transport_type, call_data = await parse_telephony_websocket(websocket)
    logger.info(f"Accepted {transport_type} call: {call_data}")

    # Initialize Twilio transport with specific call metadata
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
            # Continuously send silence when bot is not speaking to keep Twilio sync'd
            audio_out_can_send_silence=True
        )
    )

    # Initialize Gemini Live LLM Service (Native S2S)
    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="end_call",
                description="Ends the phone call. Use this when the user is done or asks to hang up.",
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
                        "description": "The full name of the contact to look up (e.g. Bernard Conley)."
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
    )

    # Define tool handlers
    async def graceful_shutdown():
        # Wait a moment for any pending audio to at least start playing
        await asyncio.sleep(0.5)
        
        # If the bot is still speaking, wait for it to finish
        max_wait = 10.0 # Safety timeout
        wait_start = asyncio.get_event_loop().time()
        while transport.is_speaking() and (asyncio.get_event_loop().time() - wait_start) < max_wait:
            await asyncio.sleep(0.1)
            
        # Small extra buffer for Twilio's network jitter
        await asyncio.sleep(0.2)
        await llm.push_frame(CancelTaskFrame(), FrameDirection.UPSTREAM)

    async def hang_up(params: FunctionCallParams):
        call_sid = call_data["call_id"]
        logger.info(f"Bot is ending the call {call_sid} via end_call tool")
        pending_hangups.add(call_sid)
        await params.result_callback({"status": "hanging_up"})
        asyncio.create_task(graceful_shutdown())

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
        await params.result_callback({"status": "success", "message": f"Transferring to {phone_number}..."})
        asyncio.create_task(graceful_shutdown())

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

    # Task to warn the bot when 1 minute remains
    async def session_warning_task(interval=540):
        await asyncio.sleep(interval)
        try:
            logger.info("Sending 1-minute session warning to bot context.")
            context.add_message(
                {"role": "developer", "content": "SYSTEM WARNING: There is only 1 minute remaining in this call. Please wrap up."}
            )
            await task.queue_frames([LLMRunFrame()])
        except Exception as e:
            logger.error(f"Failed to send session warning: {e}")

    warning_task = asyncio.create_task(session_warning_task())

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
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
        # Kick off the conversation.
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        
        # Inject date/time directly into the initial developer instruction.
        # This avoids the overhead/risk of calling update_settings during handshake.
        context.add_message(
            {"role": "developer", "content": f"SYSTEM INFO: The current date and time is {now}. Simply say: 'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Who am I speaking with today?'"}
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
