import os
import json
import urllib.request
import ssl

# The token from .env
TOKEN = os.getenv("ZAMMAD_TOKEN")
BASE_URL = "https://support.10bitworks.org/api/v1"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def test_cti_with_api_token():
    print("Testing CTI endpoint with API token...")
    # Trying the common CTI endpoint pattern
    url = f"https://support.10bitworks.org/api/v1/integration/cti/{TOKEN}"
    
    payload = {
        "event": "newCall",
        "from": "12105550199",
        "to": "12105550100",
        "direction": "in",
        "callId": "test-call-id-123",
        "user": "AI Proof of Competence"
    }
    
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx) as response:
            print(f"Status: {response.status}")
            print(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_cti_with_api_token()
