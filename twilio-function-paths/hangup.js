exports.handler = async function(context, event, callback) {
  const statusMap = {
    'answered': 'answer',
    'completed': 'hangup'
  };
  
  const zammadEvent = statusMap[event.CallStatus];

  if (!zammadEvent) {
    return callback(null, {});
  }

  // Use the full number from Twilio (including +1) to be safe
  const fromNum = event.From || "";

  const payload = {
    event: zammadEvent,
    from: fromNum,
    to: event.To,
    direction: "in", // Fixed as 'in' for this webhook
    callId: event.CallSid, // Ensure capital 'I'
    user: event.CallerName || 'Unknown'
  };

  try {
    const url = context.ZAMMAD_CTI_URL;
    console.log(`Pushing to Zammad: ${JSON.stringify(payload)}`);
    
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    
    console.log(`Zammad Response: ${response.status}`);
  } catch (err) {
    console.error('Error:', err);
  }

  return callback(null, {});
};

