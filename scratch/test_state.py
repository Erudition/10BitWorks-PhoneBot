import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test_state():
    url = os.getenv("CIVICRM_API_URL")
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Civi-Auth": f"Bearer {api_key}",
        "X-Civi-Key": site_key,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # Test StateProvince/get
    state_url = url.rstrip("/") + "/StateProvince/get"
    state_params = {
        "select": ["id", "name"],
        "where": [["name", "=", "Texas"]],
        "limit": 1
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(state_url, headers=headers, data={"params": json.dumps(state_params)})
        print("State Info:")
        print(json.dumps(resp.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(test_state())
