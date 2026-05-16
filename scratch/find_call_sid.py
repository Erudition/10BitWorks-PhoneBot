import os
from twilio.rest import Client
from loguru import logger

def main():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    if not account_sid or not auth_token:
        logger.error("Twilio credentials not found.")
        return

    client = Client(account_sid, auth_token)
    
    from_number = "+13127306695"
    logger.info(f"Searching for recent calls from {from_number}")
    
    calls = client.calls.list(from_=from_number, limit=5)
    
    if not calls:
        logger.info("No recent calls found from this number.")
    
    for call in calls:
        logger.info(f"Call Sid: {call.sid}, Status: {call.status}, Start Time: {call.start_time}")

    logger.info(f"Searching for recent messages from {from_number}")
    messages = client.messages.list(from_=from_number, limit=5)
    for msg in messages:
        logger.info(f"Message Sid: {msg.sid}, Body: {msg.body[:50]}, Date Sent: {msg.date_sent}")

if __name__ == "__main__":
    main()
