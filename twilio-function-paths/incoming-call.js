exports.handler = async function (context, event, callback) {
    const twiml = new Twilio.twiml.VoiceResponse();
    const client = context.getTwilioClient(); // Initialize Twilio Client

    const ZAMMAD_URL = context.ZAMMAD_CTI_URL;
    const LOGGER_URL = "https://zammad-log-9620.twil.io/hangup";
    const STUDIO_URL = `https://webhooks.twilio.com/v1/Accounts/${context.ACCOUNT_SID}/Flows/${context.STUDIO_FLOW_SID}`;

    console.log(`Incoming request from ${event.From} to ${event.To}. CallSid: ${event.CallSid}`);

    // GUARD: Ensure this is actually a voice call. 
    // If CallSid is missing (e.g. an SMS hitting the voice URL), exit early.
    if (!event.CallSid) {
        console.warn("Aborting Zammad CTI: No CallSid present in the request.");
        // If this was an SMS, we don't want to redirect to Studio Voice Flow
        if (event.MessageSid) {
            return callback(null, '<?xml version="1.0" encoding="UTF-8"?><Response></Response>');
        }
        twiml.redirect({ method: 'POST' }, STUDIO_URL);
        return callback(null, twiml);
    }

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
        console.log(`Pushing newCall to Zammad for SID: ${event.CallSid}`);
        await fetch(ZAMMAD_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        // 3. "Upgrade" the call to subscribe to 'answered' and 'completed' events
        // This ensures Zammad is notified even if the caller hangs up before reaching Studio.
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

