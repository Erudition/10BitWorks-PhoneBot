import os
import asyncio
import zammad_cti
from loguru import logger

async def main():
    from_number = "+13127306695"
    # Try common 'to' numbers or a generic one
    to_numbers = ["+12105470221", "+18559042954", "+12105470221"] # Add more if known
    
    # In router.js, callId was event.CallSid. If it was an SMS, it might be null or missing.
    # We'll try sending hangup with call_id=None (null) and some common ones.
    
    for to_number in to_numbers:
        logger.info(f"Attempting to clear stuck call from {from_number} to {to_number} by OMITTING callId")
        payload = {
            "event": "hangup",
            "from": from_number,
            "to": to_number,
            "direction": "in"
        }
        
        endpoint = os.getenv("ZAMMAD_CTI_ENDPOINT")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                logger.info(f"Omitted callId hangup sent successfully for {to_number}")
        except Exception as e:
            logger.error(f"Failed to clear with omitted callId for {to_number}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
