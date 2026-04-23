exports.handler = async function (context, event, callback) {
    const twiml = new Twilio.twiml.VoiceResponse();
    const client = context.getTwilioClient(); // Initialize Twilio Client

    const ZAMMAD_URL = context.ZAMMAD_CTI_URL;
    const LOGGER_URL = "https://zammad-log-9620.twil.io/hangup";
    const STUDIO_URL = "https://webhooks.twilio.com/v1/Accounts/TWILIO_ACCOUNT_SID/Flows/TWILIO_FLOW_SID";

    // 1. Determine the "User" (Recipient Label) based on the dialed number
    let recipientLabel = "Makerspace";
    if (event.To === "+12105470221") {
        recipientLabel = "Virtual Receptionist";
    } else if (event.To === "+18559042954") {
        recipientLabel = "Test Line";
    }

    // 2. Tell Zammad the call has started
    const payload = {
        event: 'newCall',
        from: event.From,
        to: event.To,
        direction: 'in',
        callId: event.CallSid,
        user: recipientLabel
    };

    try {
        // Log to Zammad immediately
        await fetch(ZAMMAD_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        // 3. "Upgrade" the call to subscribe to 'answered' and 'completed' events
        await client.calls(event.CallSid).update({
            statusCallback: LOGGER_URL,
            statusCallbackEvent: ['answered', 'completed']
        });
    } catch (err) {
        console.error("Zammad setup failed, but continuing to Studio:", err);
    }

    // 3. Hand the call over to your Studio Flow
    twiml.redirect({ method: 'POST' }, STUDIO_URL);

    return callback(null, twiml);
};
