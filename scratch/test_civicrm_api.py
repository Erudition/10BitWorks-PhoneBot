import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test_api():
    url = os.getenv("CIVICRM_API_URL")
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Civi-Auth": f"Bearer {api_key}",
        "X-Civi-Key": site_key,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # 1. First get Connor's ID using his phone number
    phone_url = url.rstrip("/") + "/Phone/get"
    phone_params = {
        "select": ["contact_id", "contact_id.display_name"],
        "where": [["phone", "LIKE", "%9738623951%"]],
        "limit": 1
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(phone_url, headers=headers, data={"params": json.dumps(phone_params)})
        data = resp.json()
        if not data.get("values"):
            print("Connor not found by phone")
            return
        contact_id = data["values"][0]["contact_id"]
        print(f"Found Connor! Contact ID: {contact_id}")
        
        # 2. Test Membership/get
        membership_url = url.rstrip("/") + "/Membership/get"
        membership_params = {
            "select": ["membership_type_id:label", "status_id:label", "start_date", "end_date"],
            "where": [["contact_id", "=", contact_id]],
            "limit": 5
        }
        resp = await client.post(membership_url, headers=headers, data={"params": json.dumps(membership_params)})
        print("\nMembership Info:")
        print(json.dumps(resp.json(), indent=2))
        
        # 3. Test Address/get
        address_url = url.rstrip("/") + "/Address/get"
        address_params = {
            "select": ["street_address", "city", "state_province_id:label", "postal_code", "location_type_id:label", "is_primary"],
            "where": [["contact_id", "=", contact_id]],
            "limit": 5
        }
        resp = await client.post(address_url, headers=headers, data={"params": json.dumps(address_params)})
        print("\nAddress Info:")
        print(json.dumps(resp.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(test_api())
