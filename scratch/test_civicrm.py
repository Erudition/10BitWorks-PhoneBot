import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

import civicrm_lookup

async def main():
    print("Testing CiviCRM API...")
    
    # 1. Test lookup by name
    name_to_search = "Connor Doherty"
    print(f"\nLooking up name: '{name_to_search}'")
    contacts = await civicrm_lookup.lookup_contact_by_name(name_to_search)
    print(f"Results for '{name_to_search}':")
    print(contacts)
    
    # 2. Extract a phone number if found, and test reverse lookup
    if contacts and contacts[0].get("phones"):
        phone_number = contacts[0]["phones"][0]["number"]
        print(f"\nLooking up phone: '{phone_number}'")
        found_name = await civicrm_lookup.lookup_contact_by_phone(phone_number)
        print(f"Result for '{phone_number}': {found_name}")

if __name__ == "__main__":
    asyncio.run(main())
