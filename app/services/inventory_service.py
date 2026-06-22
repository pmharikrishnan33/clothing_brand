import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.core.database import get_db

logger = logging.getLogger(__name__)

# --- Inventory Metadata Management ---
async def get_inventory_metadata(tenant_id: str) -> Dict[str, Any]:
    """Fetches mapping dictionaries for colors and sizes."""
    db = get_db()
    metadata = await db.inventory_metadata.find_one({"tenant_id": tenant_id})
    return metadata or {
        "color_map": {}, 
        "size_groups": {}, 
        "category_size_map": {},
        "categories": {},
        "types": {}
    }

def _hydrate_item_attributes(item: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Converts stored short keys/integers back to human-readable strings."""
    color_map = metadata.get("color_map", {})
    size_groups = metadata.get("size_groups", {})
    
    # Hydrate colors
    if "color_ids" in item:
        rev_colors = {str(v): k for k, v in color_map.items()}
        item["color"] = [rev_colors.get(str(cid), str(cid)) for cid in item.get("color_ids", [])]

    # Hydrate sizes based on category
    if "size_ids" in item:
        cat = item.get("category", "generic")
        group_name = metadata.get("category_size_map", {}).get(cat, "alpha")
        size_map = size_groups.get(group_name, {})
        rev_sizes = {str(v): k for k, v in size_map.items()}
        item["size"] = [rev_sizes.get(str(sid), str(sid)) for sid in item.get("size_ids", [])]
        
    return item

async def update_inventory_metadata(tenant_id: str, metadata: Dict[str, Any]) -> None:
    """Updates or creates inventory metadata for a tenant."""
    db = get_db()
    metadata.pop("_id", None)
    await db.inventory_metadata.update_one(
        {"tenant_id": tenant_id},
        {"$set": metadata},
        upsert=True
    )

def _prepare_item(tenant_id: str, item_data: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to structure the document with all required fields."""
    return {
        "tenant_id": tenant_id,
        "title": item_data.get("title"),
        "description": item_data.get("description"),
        "media": item_data.get("media", []),          # List of URLs
        "category": item_data.get("category"),
        "type": item_data.get("type"),
        "color": item_data.get("color", []),
        "size": item_data.get("size", []),
        "color_ids": item_data.get("color_ids", []), # Store optimized IDs
        "size_ids": item_data.get("size_ids", []),   # Store optimized IDs
        "price": item_data.get("price"),             # New: Baseline catalog price
        "stock": item_data.get("stock"),             # New: Absolute inventory count
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
    metadata = await get_inventory_metadata(tenant_id)

    for item in items:
        item["_id"] = str(item["_id"])
        _hydrate_item_attributes(item, metadata)
    return items

async def search_tenant_inventory(
    tenant_id: str, 
    category: Optional[str] = None, 
    item_type: Optional[str] = None,
    colors: Optional[List[str]] = None, 
    query: Optional[str] = None, 
    max_price: Optional[float] = None,
    limit: int = 5
) -> List[Dict[str, Any]]:
    """Searches the client-specific collection based on rules."""
    db = get_db()
    collection = db[f"inventory.{tenant_id}"]
    
    filter_doc = {}
    if category:
        filter_doc["category"] = category
    if item_type:
        filter_doc["type"] = item_type
    if colors:
        metadata = await get_inventory_metadata(tenant_id)
        cmap = metadata.get("color_map", {})
        # Convert string colors to IDs for optimized search
        color_ids = [cmap[c.lower()] for c in colors if c.lower() in cmap]
        if color_ids:
            filter_doc["color_ids"] = {"$in": color_ids}
        else:
            filter_doc["color"] = {"$in": colors}

    if query:
        filter_doc["$or"] = [
            {"title": {"$regex": query, "$options": "i"}},
            {"description": {"$regex": query, "$options": "i"}}
        ]
    if max_price is not None:
        filter_doc["price"] = {"$lte": max_price}

    cursor = collection.find(filter_doc).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    metadata = await get_inventory_metadata(tenant_id)
    for item in items:
        _hydrate_item_attributes(item, metadata)
    return items

async def get_distinct_categories(tenant_id: str) -> List[str]:
    db = get_db()
    return await db[f"inventory.{tenant_id}"].distinct("category")

def format_manual_inventory_for_ai(items: List[Dict[str, Any]]) -> str:
    """Formats manual DB items into a string for the AI prompt."""
    if not items:
        return "No matching items found in inventory."
    
    lines = []
    for item in items:
        lines.append(format_single_item_for_whatsapp(item))
    
    return "\n".join(lines)

def format_single_item_for_whatsapp(item: Dict[str, Any]) -> str:
    """Formats a single manual inventory item for WhatsApp."""
    info = f"🛍️ *{item.get('title')}*\n"
    if item.get('type'):
        info += f"🏷️ Type: {item.get('type').capitalize()}\n"
    if item.get('color'):
        info += f"🎨 Colors: {', '.join(item.get('color', []))}\n"
    if item.get('price') is not None:
        try:
            info += f"💰 Price: ₹{float(item.get('price')):.2f}\n"
        except (TypeError, ValueError):
            info += f"💰 Price: ₹{item.get('price')}\n"
    if item.get('stock') is not None:
        info += f"📦 Stock: {item.get('stock')} available\n"
    if item.get('size'):
        info += f"📏 Sizes: {', '.join(item.get('size', []))}\n"
    if item.get('description'):
        desc = item.get('description')
        info += f"📝 {desc[:75]}...\n"
    return info
