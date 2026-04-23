import os
import httpx
from loguru import logger

TOKEN = os.getenv("ZAMMAD_API_TOKEN")
BASE_URL = "https://support.10bitworks.org/api/v1"

async def create_ticket(title: str, body: str, customer: str, group_id: int = 1):
    """
    Creates a new ticket in Zammad via the REST API.
    'customer' can be an email address or a login.
    'group_id' defaults to 1 (All Support Volunteers).
    """
    if not TOKEN:
        logger.error("ZAMMAD_API_TOKEN not found in environment.")
        return None

    headers = {
        "Authorization": f"Token token={TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "title": title,
        "group_id": group_id,
        "customer": customer,
        "article": {
            "subject": title,
            "body": body,
            "type": "note",
            "internal": False
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{BASE_URL}/tickets", headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"Zammad ticket created successfully: #{result.get('number')} for {customer}")
            return result
    except Exception as e:
        logger.error(f"Failed to create Zammad ticket for {customer}: {e}")
        return None
