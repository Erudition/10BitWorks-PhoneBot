import os
import sys
from pathlib import Path
import asyncio
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
from dotenv import load_dotenv

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer

load_dotenv(override=True)

app = FastAPI()

# Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# Note: Using the model name without the "models/" prefix or v1alpha features
# was required to resolve the 1011 internal error.
MODEL_NAME = "gemini-3.1-flash-live-preview"

if not GOOGLE_API_KEY:
    logger.error("GOOGLE_API_KEY not found in environment variables")
    sys.exit(1)

# Load system prompt from file
SYSTEM_PROMPT_PATH = Path(__file__).parent / "SYSTEM_PROMPT.md"
try:
    with open(SYSTEM_PROMPT_PATH, "r") as f:
        SYSTEM_PROMPT = f.read()
    logger.info(f"Loaded system prompt from {SYSTEM_PROMPT_PATH}")
except Exception as e:
    logger.error(f"Failed to load system prompt from {SYSTEM_PROMPT_PATH}: {e}")
    sys.exit(1)

@app.get("/twiml")
async def twiml(request: Request):
    host = request.url.netloc
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://{host}/ws" />
        </Connect>
    </Response>"""
    return Response(content=twiml_response, media_type="application/xml")

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
    llm = GeminiLiveLLMService(
        api_key=GOOGLE_API_KEY,
        settings=GeminiLiveLLMService.Settings(
            model=MODEL_NAME,
            system_instruction=SYSTEM_PROMPT,
            voice="Charon",
        )
    )

    # Workaround: Proactively refresh the session before the 10-minute limit
    async def refresh_session_loop(llm_service, interval=540):
        while True:
            await asyncio.sleep(interval)
            try:
                logger.info("Proactively refreshing Gemini Live session...")
                await llm_service._reconnect()
                logger.info("Gemini Live session refreshed successfully.")
            except Exception as e:
                logger.error(f"Gemini refresh failed: {e}")

    refresh_task = asyncio.create_task(refresh_session_loop(llm))

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
            {"role": "developer", "content": "Please introduce yourself to the user and ask how you can help."}
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Client disconnected: {client}")
        refresh_task.cancel()
        await task.cancel()

    runner = PipelineRunner(handle_sigint=True)
    await runner.run(task)

if __name__ == "__main__":
    import uvicorn
    # Local dev server
    uvicorn.run(app, host="0.0.0.0", port=8000)
