import os
import sys
from pathlib import Path
import asyncio
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
from dotenv import load_dotenv

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import LLMRunFrame, EndFrame, EndTaskFrame
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

# Global state for call transfers
# In production, use a TTL cache or Redis instead of a simple dict
pending_transfers = {}

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
    # We point to our /post_bot endpoint instead of directly back to Studio
    # so we can intercept any requested transfers.
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
        auth_token=os.getenv("TWILIO_AUTH_TOKEN")
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer
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
                description="Writes to the log that a question was asked that is not in the knowledge base. Use this silently when you cannot answer a question accurately. Data will be analyzed at a later time to improve the knowledge base.",
                properties={
                    "question": {
                        "type": "string",
                        "description": "The specific question asked by the user that was not found in the knowledge base."
                    }
                },
                required=["question"]
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
                name="transfer_to_contact",
                description="Look up a contact name in the CiviCRM database and transfer the call to them. Use this only when an exact name is provided by the caller.",
                properties={
                    "contact_name": {
                        "type": "string",
                        "description": "The full name of the contact as requested by the caller (e.g. Bernard Conley)."
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
    async def hang_up(params: FunctionCallParams):
        logger.info("Bot is ending the call via end_call tool")
        await params.result_callback({"status": "hanging_up"})
        # Gemini Live requires EndTaskFrame UPSTREAM for graceful pipeline termination
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    async def notify_slack(params: FunctionCallParams):
        question = params.arguments.get("question")
        webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            logger.error("SLACK_WEBHOOK_URL not found in environment")
            await params.result_callback({"status": "error", "message": "Slack webhook not configured"})
            return

        payload = {"message": f"Receptionist fielded a question that is not answered in the current knowledge base: {question}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(webhook_url, json=payload)
                response.raise_for_status()
            logger.info(f"Notified Slack about missing knowledge: {question}")
            await params.result_callback({"status": "success", "message": "Logged for further review."})
        except Exception as e:
            logger.error(f"Failed to notify Slack: {e}")
            await params.result_callback({"status": "error", "message": str(e)})

    async def start_transfer(params: FunctionCallParams):
        phone_number = params.arguments.get("phone_number")
        call_sid = call_data["call_id"]
        logger.info(f"Bot requesting transfer to {phone_number} for call {call_sid}")
        
        # Register the transfer intent
        pending_transfers[call_sid] = phone_number
        
        await params.result_callback({"status": "success", "message": f"Transferring to {phone_number}..."})
        # End the pipeline to allow the /post_bot fallback to take over
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    async def lookup_and_transfer(params: FunctionCallParams):
        contact_name = params.arguments.get("contact_name")
        logger.info(f"Bot requesting CiviCRM lookup for: {contact_name}")
        
        try:
            # Wrap lookup in a timeout to prevent hanging the bot process
            contacts = await asyncio.wait_for(civicrm_lookup.lookup_contact_by_name(contact_name), timeout=10.0)
            
            # Success check: unique contact with unique phone
            if len(contacts) == 1 and len(contacts[0]["phones"]) == 1:
                contact = contacts[0]
                phone_number = contact["phones"][0]["number"]
                call_sid = call_data["call_id"]
                
                logger.info(f"Unique match found for {contact_name}: {phone_number}. Initiating transfer.")
                pending_transfers[call_sid] = phone_number
                await params.result_callback({"status": "success", "message": f"Transferring to {contact_name}..."})
                await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
                return

            # Disambiguation or error
            error_msg = civicrm_lookup.format_disambiguation_message(contacts)
            logger.info(f"CiviCRM lookup for {contact_name} requires disambiguation: {error_msg}")
            await params.result_callback({"status": "error", "message": error_msg})
        except asyncio.TimeoutError:
            logger.error(f"CiviCRM lookup timed out for: {contact_name}")
            await params.result_callback({"status": "error", "message": "The lookup service is currently slow. Please try again."})
        except Exception as e:
            logger.error(f"CiviCRM lookup failed: {e}")
            await params.result_callback({"status": "error", "message": str(e)})

    llm.register_function("end_call", hang_up, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("report_missing_knowledge", notify_slack, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("transfer_call", start_transfer, cancel_on_interruption=False, timeout_secs=5.0)
    llm.register_function("transfer_to_contact", lookup_and_transfer, cancel_on_interruption=False, timeout_secs=5.0)

    # Task to warn the bot when 1 minute remains (10-minute limit)
    async def session_warning_task(interval=540):
        await asyncio.sleep(interval)
        try:
            logger.info("Sending 1-minute session warning to bot context.")
            context.add_message(
                {"role": "developer", "content": "SYSTEM WARNING: There is only 1 minute remaining in this call due to a technical limit. Please politely wrap up the conversation and inform the user if necessary."}
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
            audio_in_sample_rate=8000,  # Twilio standard
            audio_out_sample_rate=8000, # Twilio standard
            enable_metrics=True,
            enable_usage_metrics=True,
        )
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected: {client}")
        # Kick off the conversation.
        context.add_message(
            {"role": "developer", "content": "Simply say: 'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Who am I speaking with today?'"}
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
    # Local dev server
    uvicorn.run(app, host="0.0.0.0", port=8000)
.0.0", port=8000)
