import os
import json
import urllib.request
import ssl
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ZAMMAD_API_TOKEN")
BASE_URL = "https://support.10bitworks.org/api/v1"

# Setup SSL context to ignore cert errors
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def main():
    if not TOKEN:
        print("Error: ZAMMAD_API_TOKEN not found.")
        return

    headers = {
        "Authorization": f"Token token={TOKEN}",
        "Content-Type": "application/json"
    }
    
    endpoints = [
        "/settings",
        "/integrations",
        "/integration/cti"
    ]
    
    for ep in endpoints:
        print(f"Checking {ep}...")
        try:
            req = urllib.request.Request(f"{BASE_URL}{ep}", headers=headers)
            with urllib.request.urlopen(req, context=ctx) as response:
                print(f"Status: {response.status}")
                data = response.read().decode('utf-8')
                print(f"Data: {data[:500]}...")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
