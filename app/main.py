from fastapi import FastAPI, HTTPException, Query, Request

from app.core.client_manager import get_client_config
from app.core.config import VERIFY_TOKEN
from app.services.message_service import message_service

app = FastAPI()

#----------GET REQUEST-----------

@app.get("/webhook")
def verify(hub_mode: str = Query(None, alias="hub.mode"), 
           hub_token: str = Query(None, alias="hub.verify_token"), 
           hub_challenge: str = Query(None, alias="hub.challenge")):
    if hub_mode == "subscribe" and hub_token == VERIFY_TOKEN:
        return hub_challenge
    raise HTTPException(status_code=403)

#----------POST REQUEST-----------

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return {"status": "ignored"}

        msg = messages[0]
        from_phone = msg["from"]
        phone_id = value["metadata"]["phone_number_id"]
        text_data = msg.get("text", {}).get("body", "") or msg.get("button", {}).get("text", "")
        if not text_data and msg.get("interactive"):
            interactive = msg.get("interactive", {})
            text_data = (
                interactive.get("button_reply", {}).get("title")
                or interactive.get("list_reply", {}).get("title")
                or ""
            )

        # Fetch the specific client configuration from DB
        client = get_client_config(phone_id)
        if not client:
            return {"status": "client_not_found"}

        await message_service(client, from_phone, text_data, phone_id)
        
        return {"status": "success"}
    except Exception as e:
        print(f"🚨 WEBHOOK ERROR: {e}")
        return {"status": "error"}
