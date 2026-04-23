import os
import httpx
from loguru import logger

from typing import Union

async def create_ticket(title: str, body: str, customer: str, group_id: int = 1, article_type: str = "phone", owner: Union[int, str] = None):
    """
    Creates a new ticket in Zammad via the REST API.
    'customer' can be an email address or a login.
    'article_type' defaults to 'phone' as requested.
    'owner' can be a user ID (int) or a login (str).
    """
    token = os.getenv("ZAMMAD_API_TOKEN")
    base_url = "https://support.10bitworks.org/api/v1"
    
    if not token:
        logger.error("ZAMMAD_API_TOKEN not found in environment.")
        return None

    headers = {
        "Authorization": f"Token token={token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "title": title,
        "group_id": group_id,
        "customer": customer,
        "article": {
            "subject": title,
            "body": body,
            "type": article_type,
            "internal": False
        }
    }
    
    if owner:
        if isinstance(owner, int):
            payload["owner_id"] = owner
        else:
            payload["owner"] = owner

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{base_url}/tickets", headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"Zammad ticket created successfully: #{result.get('number')} for {customer} (Owner: {owner})")
            return result
    except Exception as e:
        logger.error(f"Failed to create Zammad ticket for {customer}: {e}")
        return None
