import os
import json
import urllib.request
import ssl

TOKEN = "s1eCnb9csmGZ9r3vsUQUUaLFmT8DGVWuvxCE8FG7EZ3rYfA3LstHbFC7v11PKVte"
BASE_URL = "https://support.10bitworks.org/api/v1"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def simulate_call():
    print("Simulating a phone call ticket in Zammad...")
    
    url = f"{BASE_URL}/tickets"
    headers = {
        "Authorization": f"Token token={TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Trying 'phone' as the type instead of 'phone-in'
    payload = {
        "title": "SIMULATED CALL via 10Bot (AI Proof of Competence)",
        "group": "All Support Volunteers",
        "customer_id": "guess:test-caller@example.com",
        "article": {
            "subject": "Incoming Call Summary",
            "body": "This is a simulated call record created to demonstrate API competence.",
            "type": "phone",
            "internal": False
        }
    }
    
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            print(f"Status: {response.status}")
            data = json.loads(response.read().decode('utf-8'))
            print(f"Success! Ticket Created. ID: {data.get('id')}")
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} {e.reason}")
        print(e.read().decode('utf-8'))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    simulate_call()
