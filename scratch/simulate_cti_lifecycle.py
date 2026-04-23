import json
import urllib.request
import ssl
import time
import os

# From .env
CTI_ENDPOINT = "https://support.10bitworks.org/api/v1/cti/q97LytVWD7gI9qiTiZKtL87fFI0"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def push_event(event_data):
    print(f"[{time.strftime('%H:%M:%S')}] Sending event: {event_data['event']}...")
    try:
        req = urllib.request.Request(
            CTI_ENDPOINT,
            data=json.dumps(event_data).encode('utf-8'),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, context=ctx) as response:
            print(f"Status: {response.status}")
    except Exception as e:
        print(f"Error sending {event_data['event']}: {e}")

def main():
    call_id = f"simulated-call-{int(time.time())}"
    from_num = "+18482180683" # Added +1
    to_num = "+12105470221"
    
    # 1. New Call
    push_event({
        "event": "newCall",
        "from": from_num,
        "to": to_num,
        "direction": "in",
        "callId": call_id,
        "user": "Connor (Simulated +1)" 
    })
    
    print("Waiting 20 seconds (Ringing stage)...")
    time.sleep(20)
    
    # 2. Answer
    push_event({
        "event": "answer",
        "from": from_num,
        "to": to_num,
        "direction": "in",
        "callId": call_id,
        "answeringNumber": "+12105470221"
    })
    
    print("Holding call open for 20 seconds (Talking stage)...")
    time.sleep(20)
    
    # 3. Hangup
    push_event({
        "event": "hangup",
        "from": from_num,
        "to": to_num,
        "direction": "in",
        "callId": call_id
    })
    print("Simulation complete.")

if __name__ == "__main__":
    main()
