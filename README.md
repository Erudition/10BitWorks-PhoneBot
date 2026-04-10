# Phone Assistant with Gemini Live 3.1 and Pipecat

This project implements a locally hosted Pipecat instance that routes audio between Twilio and the Gemini Live API using the Gemini 3.1 live audio model.

## Prerequisites

1. **Python 3.10+**
2. **Twilio Account**: A phone number with voice capabilities.
3. **Google AI Studio API Key**: Access to the Gemini API.
4. **ngrok**: To expose your local server to the internet.

## Installation

1. Clone the repository and navigate to the project directory.
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and add your `GOOGLE_API_KEY`:
   ```bash
   cp .env.example .env
   ```

## Running the Bot

1. Start the FastAPI server:
   ```bash
   python bot.py
   ```
2. In a separate terminal, expose the local server using ngrok:
   ```bash
   ngrok http 17293
   ```
3. Note the ngrok forwarding URL (e.g., `https://xxxx-xxxx.ngrok-free.app`).

## Twilio Configuration

1. Log in to your [Twilio Console](https://www.twilio.com/console).
2. Navigate to **Phone Numbers** > **Manage** > **Active numbers**.
3. Select your phone number.
4. Under **Voice & Fax**, set the "A CALL COMES IN" webhook to:
   - **Method**: HTTP GET
   - **URL**: `https://your-ngrok-url.ngrok-free.app/twiml` (Replace with your actual ngrok URL).
5. Save the configuration.

## How it Works

- When a call comes in, Twilio sends a GET request to `/twiml`.
- The server responds with TwiML instructions to connect the call to a WebSocket stream (`/ws`).
- Pipecat's `FastAPIWebsocketTransport` handles the bidirectional audio stream from Twilio.
- The `GeminiLiveLLMService` interacts with `models/gemini-3.1-flash-live-preview` to provide real-time speech-to-speech AI interaction.
