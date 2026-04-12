import httpx
import json
import os
from loguru import logger

async def get_membership_info(contact_id: int):
    """
    Returns a summary of the contact's memberships.
    """
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        return "CiviCRM API not configured."
    
    endpoint = url.rstrip("/") + "/Membership/get"
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Civi-Auth": f"Bearer {api_key}",
        "X-Civi-Key": site_key,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    params = {
        "select": ["membership_type_id:label", "status_id:label", "end_date"],
        "where": [["contact_id", "=", contact_id]],
        "orderBy": {"end_date": "DESC"},
        "limit": 3
    }
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(endpoint, headers=headers, data={"params": json.dumps(params)})
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("values"):
                return "No membership records found for this contact."
            
            summary = "Membership status:\n"
            for m in data["values"]:
                status = m.get("status_id:label", "Unknown")
                m_type = m.get("membership_type_id:label", "Unknown")
                end_date = m.get("end_date", "N/A")
                summary += f"- {m_type}: {status} (Expires: {end_date})\n"
            return summary
            
    except Exception as e:
        logger.error(f"Membership lookup failed: {e}")
        return "Error retrieving membership information."

async def get_address_info(contact_id: int):
    """
    Returns the primary address for the contact.
    """
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        return "CiviCRM API not configured."
    
    endpoint = url.rstrip("/") + "/Address/get"
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Civi-Auth": f"Bearer {api_key}",
        "X-Civi-Key": site_key,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    params = {
        "select": ["id", "street_address", "city", "state_province_id:label", "postal_code", "is_primary"],
        "where": [["contact_id", "=", contact_id]],
        "limit": 5
    }
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(endpoint, headers=headers, data={"params": json.dumps(params)})
            resp.raise_for_status()
            data = resp.json()
            
            if not data.get("values"):
                return "No address on file."
            
            # Prefer primary
            addr_list = data["values"]
            primary = next((a for m in addr_list if (a := m).get("is_primary")), addr_list[0])
            
            return {
                "id": primary.get("id"),
                "display": f"{primary.get('street_address')}, {primary.get('city')}, {primary.get('state_province_id:label')} {primary.get('postal_code')}",
                "street": primary.get("street_address"),
                "city": primary.get("city"),
                "zip": primary.get("postal_code")
            }
            
    except Exception as e:
        logger.error(f"Address lookup failed: {e}")
        return "Error retrieving address."

async def update_address(contact_id: int, street_address: str, city: str, postal_code: str):
    """
    Updates the primary address for the contact.
    """
    url = os.getenv("CIVICRM_API_URL")
    if not url:
        return "CiviCRM API not configured."
    
    # First get existing address to see if we update or create
    existing = await get_address_info(contact_id)
    
    endpoint = url.rstrip("/") + "/Address/save"
    api_key = os.getenv("CIVICRM_API_KEY")
    site_key = os.getenv("CIVICRM_SITE_KEY")
    
    headers = {
        "X-Civi-Auth": f"Bearer {api_key}",
        "X-Civi-Key": site_key,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    record = {
        "contact_id": contact_id,
        "street_address": street_address,
        "city": city,
        "postal_code": postal_code,
        "state_province_id": 1042, # Texas
        "is_primary": True,
        "location_type_id": 1 # Home
    }
    
    # If we have an existing ID, include it to update rather than create another
    if isinstance(existing, dict) and existing.get("id"):
        record["id"] = existing["id"]
        
    params = {
        "records": [record]
    }
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(endpoint, headers=headers, data={"params": json.dumps(params)})
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("is_error"):
                return f"Failed to update address: {data.get('error_message')}"
            
            return "Address updated successfully."
            
    except Exception as e:
        logger.error(f"Address update failed: {e}")
        return "Error updating address."
