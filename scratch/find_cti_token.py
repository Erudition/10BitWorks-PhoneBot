import os
import json
import urllib.request
import ssl

TOKEN = os.getenv("ZAMMAD_TOKEN")
BASE_URL = "https://support.10bitworks.org/api/v1"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def list_integrations():
    url = f"{BASE_URL}/integration" # Trial and error for the endpoint
    # Or /integration/cti
    headers = {
        "Authorization": f"Token token={TOKEN}",
        "Content-Type": "application/json"
    }
    
    for ep in ["/integration", "/integration/cti", "/integration/cti_generic"]:
        print(f"Checking {ep}...")
        try:
            req = urllib.request.Request(f"{BASE_URL}{ep}", headers=headers)
            with urllib.request.urlopen(req, context=ctx) as response:
                data = json.loads(response.read().decode('utf-8'))
                print(f"Data for {ep}: {json.dumps(data, indent=2)}")
        except Exception as e:
            print(f"Error for {ep}: {e}")

if __name__ == "__main__":
    list_integrations()
