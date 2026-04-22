import os
import httpx
from loguru import logger

async def push_cti_event(event: str, from_number: str, to_number: str, direction: str, call_id: str, user_name: str = None, answering_number: str = None):
    """
    Pushes a CTI event to Zammad.
    """
    endpoint = os.getenv("ZAMMAD_CTI_ENDPOINT")
    if not endpoint:
        logger.warning("ZAMMAD_CTI_ENDPOINT not found in environment. Skipping Zammad CTI update.")
        return

    payload = {
        "event": event,
        "from": from_number,
        "to": to_number,
        "direction": direction,
        "callId": call_id
    }
    
    if user_name:
        payload["user"] = user_name
    
    if answering_number:
        payload["answeringNumber"] = answering_number

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            logger.debug(f"Zammad CTI event '{event}' pushed successfully for call {call_id}.")
    except Exception as e:
        logger.error(f"Failed to push Zammad CTI event '{event}': {e}")

async def log_new_call(from_number: str, to_number: str, call_id: str, user_name: str = "Assistant"):
    await push_cti_event("newCall", from_number, to_number, "in", call_id, user_name=user_name)

async def log_answer(from_number: str, to_number: str, call_id: str, answering_number: str = "10Bot"):
    await push_cti_event("answer", from_number, to_number, "in", call_id, answering_number=answering_number)

async def log_hangup(from_number: str, to_number: str, call_id: str):
    await push_cti_event("hangup", from_number, to_number, "in", call_id)
