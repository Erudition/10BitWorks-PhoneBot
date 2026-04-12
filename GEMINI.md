# Phone Assistant Project Guidelines & Lessons Learned

This file documents critical architectural decisions, workarounds, and gotchas discovered during the development of the 10BitWorks Phone Assistant using Pipecat, Twilio, and the Gemini 3.1 Live API. Future agents modifying this codebase must adhere to these guidelines to prevent regressions.

## 1. Gemini 3.1 Live Integration Constraints
*   **Model String**: The correct model string for Gemini 3.1 Live is `gemini-3.1-flash-live-preview`. Do **NOT** use the `models/` prefix. Using the prefix or incorrect strings will cause a `1011 Internal error` during connection.
*   **API Versioning**: Do not force the `v1alpha` API version or use `enable_affective_dialog=True` in the `GeminiLiveLLMService.Settings`. These features are specific to Gemini 2.5 Native Audio models. Stick to the default `v1beta` (which Pipecat uses automatically when `http_options` is omitted).
*   **Dynamic Prompting (Date/Time)**: Do not inject dynamic data (like the current time) as a fake "user" message in the conversation history, as this causes the model to stall. Instead, prepend it to the `system_instruction` during the `on_client_connected` event via `llm.update_settings(GeminiLiveLLMService.Settings(system_instruction=...))`.

## 2. Tool Calling with Gemini 3.1 Live
*   **No Asynchronous Function Calling**: Gemini 3.1 Flash Live Preview does **not** currently support native asynchronous tool calling.
    *   *Workaround*: If a tool needs to perform a network request (e.g., posting to Slack), the tool handler *must* return a success payload to the LLM immediately (via `await params.result_callback(...)`) and dispatch the actual work to a background task (e.g., `asyncio.create_task(send_to_slack(...))`). This prevents the bot from "stuttering" or hanging while waiting for the tool to finish.
*   **Separation of Lookup and Action**: Do not combine information gathering (lookup) and state mutation (e.g., transferring a call) into a single tool. Gemini may speculatively call a tool multiple times to gather options for the user.
    *   *Example*: Use `lookup_contact` to get a phone number, then require the bot to explicitly call `transfer_call` after presenting options to the user.
*   **Immediate Hangup Prompts**: To ensure the bot hangs up in the same conversational turn as its farewell, the tool description for `end_call` must include strong directives (e.g., "CRITICAL: You MUST call this tool immediately in the exact same turn after you say goodbye...").
*   **Timeouts**: Wrap all external network requests (like CiviCRM lookups) in an `asyncio.wait_for` timeout (e.g., 4.5 seconds). Pipecat enforces a strict 5.0-second timeout for tool execution, and catching it at 4.5s allows us to gracefully return an error to the LLM instead of crashing the pipeline. Also, pass `timeout_secs=5.0` to `llm.register_function`.

## 3. Twilio Audio Pacing and Stability
*   **Strict 20ms Pacing**: Twilio Media Streams expect 8kHz, 16-bit mono audio (320 bytes = 20ms chunks). Use Pipecat's native `audio_out_10ms_chunks=2` setting in `FastAPIWebsocketParams`.
*   **Avoid External Schedulers**: Do not use `FixedSizeScheduler` or `prefatory_silence_threshold`. They interfere with the native chunking and cause "clicking" or "speed-run" distorted audio.
*   **Avoid Infinite Silence**: Do NOT set `audio_out_can_send_silence=True`. While it keeps the stream synchronized, Pipecat will dump infinite silence into the buffer while the LLM is "thinking", causing massive (multi-second) delays before the bot's actual speech is heard.

## 4. Graceful Shutdown & The 30-Second Gemini Bug
*   **The Bug**: Gemini 3.1 Live defers the processing of `EndFrame` for 30 seconds after the bot finishes speaking, which causes the Twilio call to hang in silence if a standard `EndTaskFrame` is used.
*   **The Fix (Wait-and-Cancel)**:
    1.  Use a custom `FrameProcessor` (e.g., `SpeechTracker`) that intercepts `BotStartedSpeakingFrame` and `BotStoppedSpeakingFrame` to accurately track when the bot is outputting audio.
    2.  When a tool like `end_call` or `transfer_call` is triggered, launch a background task (`wait_and_terminate`).
    3.  This task must poll the `SpeechTracker` and wait for a contiguous period of silence (e.g., 1.5 seconds) to ensure the bot's trailing "room tone" has fully flushed to Twilio.
    4.  Finally, push a `CancelTaskFrame()` UPSTREAM to immediately kill the pipeline and bypass the 30-second Gemini hang.
*   **Twilio Studio Fallback**: To allow Twilio to fall back to a Studio Flow `<Redirect>` after the bot hangs up or transfers:
    *   Set `auto_hang_up=False` in the `TwilioFrameSerializer`. This prevents Pipecat from terminating the call via the REST API.
    *   Maintain a global state (e.g., `pending_hangups`, `pending_transfers`).
    *   When the WebSocket closes (due to the `CancelTaskFrame`), Twilio hits your `/post_bot` TwiML endpoint. Check the global state there to return either a `<Dial>` or a `<Hangup>`/`<Redirect>`.

## 5. Scope and Initialization
*   Always define tool handlers and background tasks *after* the `llm` and `transport` objects are fully instantiated within the async endpoint. Defining them beforehand leads to `UnboundLocalError` crashes because the closure attempts to capture variables that aren't fully initialized.

## 6. Context Management & Prompt Injection
*   **Avoid Aggressive `LLMRunFrame`**: When injecting silent warnings or background context updates into the conversation history (e.g., a "time remaining" warning), do **NOT** queue an `LLMRunFrame()` immediately afterward. `LLMRunFrame()` mechanically forces the LLM to generate speech *right now*. If the bot has nothing conversational to say, this will cause it to abruptly interrupt the caller or "panic-speak" hallucinated text (like reciting random parts of its prompt). Simply use `context.add_message(...)` and let the bot naturally see the new context during its next regular conversational turn.
*   **Caller Profile Injection**: For recognized contacts, the `on_client_connected` handler fetches a full CiviCRM profile (membership status with join/start/end dates, all addresses, phones, and emails) and injects it into a `CURRENT CALLER INFO` block in the initial developer prompt. This allows the bot to answer personal account questions instantly without tool calls.

## 7. CiviCRM Contact Management
*   **Create Contact**: The bot proactively creates new contact records for unrecognized callers using the `create_my_contact_record` tool as soon as they provide a first and last name. This ensures all inquiries are accurately logged in CiviCRM.
*   **Safe Updates**: Data management tools (address, phone, email) are "add-only" or "primary-toggle" to prevent accidental deletion or overwriting of existing records. The bot cannot delete records.
*   **Membership Intelligence**: Membership info now includes `join_date` and `start_date` alongside `end_date`, providing the bot with full context on the user's history with the makerspace.
