import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Header
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from app.api.routes.usage import router as usage_router
from app.core.client_manager import get_client_config
from app.core.config import VERIFY_TOKEN, APP_SECRET
from app.services.message_service import message_service
from app.services.shopify_service import fetch_clothing_inventory, format_products_for_ai

app = FastAPI()
logger = logging.getLogger(__name__)
app.include_router(usage_router)

#----------GET REQUEST-----------

@app.get("/webhook")
def verify(hub_mode: str = Query(None, alias="hub.mode"), 
           hub_token: str = Query(None, alias="hub.verify_token"), 
           hub_challenge: str = Query(None, alias="hub.challenge")):
    if hub_mode == "subscribe" and hub_token == VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403)

@app.get("/test-shopify-integration")
async def test_shopify(q: str = "shirt", phone_id: Optional[str] = None):
    """Debug endpoint to verify Shopify fetching logic."""
    s_url, s_token, s_ver = None, None, None
    if phone_id:
        client = get_client_config(phone_id)
        if client:
            from app.core.config import clean_shopify_url
            s_url = clean_shopify_url(client.get("shopify_url", ""))
            s_token = client.get("shopify_access_token", "")
            s_ver = client.get("shopify_api_version", "2024-01")

    products = await fetch_clothing_inventory(shop_url=s_url, access_token=s_token, api_version=s_ver, query=q)
    summary = format_products_for_ai(products)
    return {"query": q, "count": len(products), "ai_summary": summary, "raw_data": products[:2]}

#----------POST REQUEST-----------

def verify_signature(payload: bytes, signature: str) -> bool:
    if not APP_SECRET or not signature:
        return False
    # Meta prefixes the signature with sha256=
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(
        APP_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

@app.post("/webhook")
async def webhook(request: Request, x_hub_signature_256: str = Header(None)):
    payload = await request.body()
    
    if not verify_signature(payload, x_hub_signature_256):
        logger.warning("Webhook signature verification failed.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = json.loads(payload)
    
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            logger.info("Webhook ignored: no messages in payload")
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
            logger.error("Client not found for phone_number_id=%s", phone_id)
            return {"status": "client_not_found"}

        result = await message_service(client, from_phone, text_data, phone_id)
        
        return result
    except Exception as e:
        logger.exception("Webhook error")
        return {"status": "error"}