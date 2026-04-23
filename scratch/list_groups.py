import os
import json
import urllib.request
import ssl

TOKEN = "s1eCnb9csmGZ9r3vsUQUUaLFmT8DGVWuvxCE8FG7EZ3rYfA3LstHbFC7v11PKVte"
BASE_URL = "https://support.10bitworks.org/api/v1"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def list_groups():
    url = f"{BASE_URL}/groups"
    headers = {
        "Authorization": f"Token token={TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            data = json.loads(response.read().decode('utf-8'))
            print("Available Groups:")
            for group in data:
                print(f"- {group['name']} (ID: {group['id']})")
    except Exception as e:
        print(f"Error listing groups: {e}")

if __name__ == "__main__":
    list_groups()
