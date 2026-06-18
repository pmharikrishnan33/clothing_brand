import hashlib
import hmac
import json
import logging
from pathlib import Path
from bson import ObjectId
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from app.core.database import get_db
from app.api.routes.usage import router as usage_router
from app.api.routes.inventory import router as inventory_router
from app.core.client_manager import get_client_config
from app.core.database import init_db
from app.core.config import VERIFY_TOKEN, APP_SECRET, clean_shopify_url
from app.services.message_service import message_service
from app.services.inventory_service import get_inventory_by_tenant, get_inventory_metadata, update_inventory_metadata
from app.services.pricing_service import usage_summary
from app.services.ai_service import generate_system_prompt
from app.services.shopify_service import fetch_clothing_inventory, format_products_for_ai

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Trigger database connection on startup to show the connection status in terminal
    try:
        await init_db()
    except Exception as e:
        print(f"🔥 Database initialization failed: {e}")
    yield

app = FastAPI(lifespan=lifespan)
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows requests from port 5500 to port 8000
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(usage_router)
app.include_router(inventory_router)

# --- ADMIN DASHBOARD API ---

@app.get("/api/admin/clients")
async def list_clients():
    db = get_db()
    clients = await db.clients.find({}).to_list(length=100)
    logger.info(f"Dashboard requested clients. Found {len(clients)} documents in 'clients' collection.")
    
    for c in clients:
        c["_id"] = str(c["_id"])
        # Ensure tenant_id is never null for the UI table
        if "tenant_id" not in c:
            c["tenant_id"] = "Unnamed Client"
    return clients

@app.post("/api/admin/clients")
async def upsert_client(client_data: dict):
    db = get_db()
    tenant_id = client_data.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")
    client_data.pop("_id", None)
    await db.clients.update_one({"tenant_id": tenant_id}, {"$set": client_data}, upsert=True)
    return {"status": "success"}

@app.delete("/api/admin/clients/{tenant_id}")
async def delete_client(tenant_id: str):
    db = get_db()
    await db.clients.delete_one({"tenant_id": tenant_id})
    await db.keywords.delete_one({"tenant_id": tenant_id})
    return {"status": "deleted"}

@app.get("/api/admin/keywords/{tenant_id}")
async def get_keywords(tenant_id: str):
    db = get_db()
    doc = await db.keywords.find_one({"tenant_id": tenant_id})
    return doc or {"tenant_id": tenant_id, "rules": []}

@app.post("/api/admin/keywords/{tenant_id}")
async def update_keywords(tenant_id: str, data: dict):
    db = get_db()
    rules = data.get("rules", [])
    await db.keywords.update_one({"tenant_id": tenant_id}, {"$set": {"rules": rules}}, upsert=True)
    return {"status": "success"}

@app.get("/api/admin/learned/{tenant_id}")
async def get_learned(tenant_id: str):
    db = get_db()
    doc = await db.learned_keywords.find_one({"tenant_id": tenant_id})
    return doc or {"tenant_id": tenant_id, "learned": []}

@app.post("/api/admin/learned/{tenant_id}")
async def update_learned(tenant_id: str, data: dict):
    db = get_db()
    learned = data.get("learned", [])
    await db.learned_keywords.update_one({"tenant_id": tenant_id}, {"$set": {"learned": learned}}, upsert=True)
    return {"status": "success"}

@app.get("/api/admin/usage/{tenant_id}")
async def get_tenant_usage(tenant_id: str, start: Optional[str] = None, end: Optional[str] = None):
    """Provides AI and Meta usage summary for the dashboard."""
    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    return await usage_summary(tenant_id, start_dt, end_dt)

@app.get("/api/admin/metadata/{tenant_id}")
async def get_metadata(tenant_id: str):
    """Fetches inventory metadata (colors/sizes mappings) for the admin panel."""
    return await get_inventory_metadata(tenant_id)

@app.post("/api/admin/metadata/{tenant_id}")
async def post_metadata(tenant_id: str, data: dict):
    """Updates inventory metadata mappings."""
    await update_inventory_metadata(tenant_id, data)
    return {"status": "success"}

@app.post("/api/admin/onboarding/{tenant_id}")
async def onboarding_setup(tenant_id: str, data: dict):
    """
    Automated compilation pipeline for shop profile.
    Generates and saves the ai_system_prompt based on brand indicators.
    """
    db = get_db()
    brand = data.get("brand_identity", "Premium Boutique")
    audience = data.get("target_audience", "fashion enthusiasts")
    tone = data.get("communication_tone", "warm and helpful")
    
    persona = generate_system_prompt(brand, audience, tone)
    
    await db.clients.update_one(
        {"tenant_id": tenant_id},
        {"$set": {"ai_system_prompt": persona, "onboarding_completed": True}},
        upsert=True
    )
    return {"status": "success", "compiled_prompt": persona}

@app.get("/api/admin/messages/{tenant_id}")
async def get_messages(tenant_id: str, limit: int = 50):
    db = get_db()
    msgs = await db.messages.find({"tenant_id": tenant_id}).sort("_id", -1).to_list(length=limit)
    for m in msgs:
        m["_id"] = str(m["_id"])
    return msgs

@app.get("/api/admin/inventory/{tenant_id}")
async def admin_get_inventory(tenant_id: str):
    items = await get_inventory_by_tenant(tenant_id, limit=100)
    return {"items": items}

@app.patch("/api/admin/inventory/{tenant_id}/{item_id}")
async def update_inventory_item(tenant_id: str, item_id: str, item_data: dict):
    db = get_db()
    item_data.pop("_id", None)
    item_data.pop("tenant_id", None)
    try:
        await db[f"inventory.{tenant_id}"].update_one(
            {"_id": ObjectId(item_id)},
            {"$set": item_data}
        )
        return {"status": "updated"}
    except:
        raise HTTPException(status_code=400, detail="Invalid Item ID")

@app.delete("/api/admin/inventory/{tenant_id}/{item_id}")
async def delete_inventory_item(tenant_id: str, item_id: str):
    db = get_db()
    try:
        await db[f"inventory.{tenant_id}"].delete_one({"_id": ObjectId(item_id)})
        return {"status": "deleted"}
    except:
        raise HTTPException(status_code=400, detail="Invalid Item ID")

# --- MOUNT DASHBOARD FROM ROOT FE FOLDER ---

base_dir = Path(__file__).resolve().parent.parent
fe_dir = base_dir / "FE"

if fe_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(fe_dir), html=True), name="dashboard")

# Handle favicon requests to prevent 404 errors in production logs
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
async def favicon():
    file_path = Path("static/favicon.png")
    return FileResponse(file_path) if file_path.exists() else PlainTextResponse("", status_code=204)

@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint to verify the API is online."""
    return {"status": "online", "service": "Zyphor Backend"}

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
        client = await get_client_config(phone_id)
        if client:
            s_url = clean_shopify_url(client.get("shopify_url") or client.get("shopify_store_url") or "")
            s_token = client.get("shopify_access_token", "")
            s_ver = client.get("shopify_api_version", "2024-01")

    products = await fetch_clothing_inventory(shop_url=s_url, access_token=s_token, api_version=s_ver, query=q)
    summary = format_products_for_ai(products, shop_url=s_url)
    return {"query": q, "count": len(products), "ai_summary": summary, "raw_data": products[:2]}

#----------POST REQUEST-----------

def verify_signature(payload: bytes, signature: str) -> bool:
    # Allow bypassing signature check in local development if desired
    # if os.getenv("ENVIRONMENT") == "development":
    #     return True
        
    if not APP_SECRET or not signature:
        logger.error("Missing APP_SECRET or signature header")
        return False
    # Meta prefixes the signature with sha256=
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(
        APP_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    is_valid = hmac.compare_digest(expected, signature)
    if not is_valid:
        logger.error(f"Signature mismatch. Expected: {expected}, Got: {signature}")
    return is_valid

@app.post("/webhook")
async def webhook(request: Request, x_hub_signature_256: str = Header(None)):
    payload = await request.body()
    logger.info(f"Received Webhook Payload: {payload.decode('utf-8')}")
    
    if not verify_signature(payload, x_hub_signature_256):
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
        client = await get_client_config(phone_id)
        if not client:
            logger.error("Client not found for phone_number_id=%s", phone_id)
            return {"status": "client_not_found"}

        result = await message_service(client, from_phone, text_data, phone_id)
        
        return result
    except Exception as e:
        logger.exception("Webhook error")
        return {"status": "error"}