import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.core.database import get_db

logger = logging.getLogger(__name__)

def _prepare_item(tenant_id: str, item_data: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to structure the document with all required fields."""
    return {
        "tenant_id": tenant_id,
        "title": item_data.get("title"),
        "description": item_data.get("description"),
        "media": item_data.get("media", []),          # List of URLs
        "category": item_data.get("category"),
        "size": item_data.get("size", []),            # List of sizes (e.g., ["S", "M"])
        "color": item_data.get("color", []),          # List of colors (e.g., ["Blue", "Black"])
        "age_group": item_data.get("age_group"),
        "care_instructions": item_data.get("care_instructions"),
        "target_customers": item_data.get("target_customers"),
        "created_at": datetime.now(timezone.utc)
    }

async def add_inventory_item(tenant_id: str, item_data: Dict[str, Any]) -> str:
    """
    Adds a single clothing item to the client-specific inventory collection.
    """
    db = get_db()
    document = _prepare_item(tenant_id, item_data)
    # Insert into client-specific collection
    result = await db[f"inventory.{tenant_id}"].insert_one(document)
    return str(result.inserted_id)

async def bulk_add_inventory_items(tenant_id: str, items_list: List[Dict[str, Any]]) -> List[str]:
    """
    Handles the case where there are multiple clothes. 
    Inserts them as separate documents in the collection.
    """
    if not items_list:
        return []
    
    db = get_db()
    documents = [_prepare_item(tenant_id, item) for item in items_list]
    result = await db[f"inventory.{tenant_id}"].insert_many(documents)
    return [str(i) for i in result.inserted_ids]

async def get_inventory_by_tenant(tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Retrieves all inventory items belonging to a specific client (tenant).
    """
    db = get_db()
    # Access the client-specific collection directly
    collection = db[f"inventory.{tenant_id}"]
    cursor = collection.find().sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    
    for item in items:
        item["_id"] = str(item["_id"])
        
    return items

async def search_tenant_inventory(
    tenant_id: str, 
    category: Optional[str] = None, 
    colors: Optional[List[str]] = None, 
    query: Optional[str] = None, 
    limit: int = 5
) -> List[Dict[str, Any]]:
    """Searches the client-specific collection based on rules."""
    db = get_db()
    collection = db[f"inventory.{tenant_id}"]
    
    filter_doc = {}
    if category:
        filter_doc["category"] = category
    if colors:
        filter_doc["color"] = {"$in": colors}
    if query:
        filter_doc["$or"] = [
            {"title": {"$regex": query, "$options": "i"}},
            {"description": {"$regex": query, "$options": "i"}}
        ]

    cursor = collection.find(filter_doc).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)

def format_manual_inventory_for_ai(items: List[Dict[str, Any]]) -> str:
    """Formats manual DB items into a string for the AI prompt."""
    if not items:
        return "No matching items found in inventory."
    
    lines = []
    for item in items:
        info = f"🛍️ *{item.get('title')}*\n"
        info += f"🎨 Colors: {', '.join(item.get('color', []))}\n"
        info += f"📏 Sizes: {', '.join(item.get('size', []))}\n"
        if item.get('description'):
            desc = item.get('description')
            info += f"📝 {desc[:75]}...\n"
        lines.append(info)
    
    return "\n".join(lines)