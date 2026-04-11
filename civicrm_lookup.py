import httpx
import json
import os
import re
from loguru import logger

async def lookup_contact_by_name(full_name: str):
    """
    Queries CiviCRM for a contact by display_name and returns their phone numbers.
    Uses APIv4 chaining to get all phones in a single call.
    """
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        logger.error("CIVICRM_API_URL not set")
        return []
    
    # Ensure URL ends with the correct endpoint for Contact.get
    if not url.endswith("Contact/get"):
        url = url.rstrip("/") + "/Contact/get"
    
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Civi-Auth": f"Bearer {api_key}",
        "X-Civi-Key": site_key,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    # APIv4 query with chaining for Phone records
    params = {
        "select": ["display_name", "first_name", "last_name"],
        "where": [["display_name", "=", full_name]],
        "limit": 5,
        "chain": {
            "phones": ["Phone", "get", {
                "where": [["contact_id", "=", "$id"]],
                "select": ["phone", "location_type_id:label", "is_primary"]
            }]
        }
    }
    
    # CiviCRM REST often expects 'params' as a form field containing JSON
    body = {
        "params": json.dumps(params)
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, data=body)
            response.raise_for_status()
            data = response.json()
            
            if data.get("is_error"):
                logger.error(f"CiviCRM API error: {data.get('error_message')}")
                return []
            
            contacts = []
            for val in data.get("values", []):
                contact = {
                    "display_name": val.get("display_name"),
                    "phones": []
                }
                
                for phone_rec in val.get("phones", []):
                    # Sanitize phone number (strip non-digits, but keep + for E.164)
                    raw_phone = phone_rec.get("phone", "")
                    clean_phone = re.sub(r'[^\d+]', '', raw_phone)
                    
                    if clean_phone:
                        contact["phones"].append({
                            "number": clean_phone,
                            "label": phone_rec.get("location_type_id:label", "Other"),
                            "is_primary": phone_rec.get("is_primary", False)
                        })
                
                contacts.append(contact)
            
            return contacts
            
    except Exception as e:
        logger.error(f"Failed to lookup CiviCRM contact: {e}")
        return []

def format_disambiguation_message(contacts):
    """
    Helps format a message for the LLM when multiple options are found.
    """
    if not contacts:
        return "No contact found with that exact name."
    
    if len(contacts) > 1:
        names = [c["display_name"] for c in contacts]
        return f"I found multiple contacts matching that name: {', '.join(names)}. Could you be more specific?"
    
    contact = contacts[0]
    phones = contact["phones"]
    
    if not phones:
        return f"I found {contact['display_name']}, but they don't have a phone number on file."
    
    if len(phones) == 1:
        return None # Success!
    
    # Multiple phones for one contact
    options = [f"{p['label']}: {p['number']}" for p in phones]
    return f"I found multiple numbers for {contact['display_name']}: {', '.join(options)}. Which one should I call?"
