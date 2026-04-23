import asyncio
import os
import sys
from dotenv import load_dotenv

# Add parent dir to path to import local modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zammad_agent

async def main():
    load_dotenv(override=True)
    
    # Try using '10bot' as the customer/login
    print("Attempting to create a test ticket assigned to 'agent 10bot'...")
    
    # We use a placeholder phone number for the test
    caller_number = "+12105550100"
    
    # Create the ticket
    result = await zammad_agent.create_ticket(
        title=f"TEST: Phone Call Transcript ({caller_number})",
        body=f"This is a manual test of the automated ticketing system.\n\n[Transcript]\n**Bot**: Hello, how can I help you?\n**User**: Is the laser cutter working?",
        customer=caller_number,
        owner="10bot@10bitworks.org",
        article_type="phone"
    )
    
    if result:
        print(f"Success! Ticket #{result.get('number')} created.")
    else:
        print("Failed to create ticket.")

if __name__ == "__main__":
    asyncio.run(main())
