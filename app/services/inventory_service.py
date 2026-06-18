import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.core.database import get_db
from bson import ObjectId

logger = logging.getLogger(__name__)

# --- Inventory Metadata Management ---
async def _get_inventory_metadata(tenant_id: str) -> Dict[str, Any]:
    """Fetches the inventory metadata for a given tenant."""
    db = get_db()
    metadata = await db.inventory_metadata.find_one({"tenant_id": tenant_id})
    if not metadata:
        # Provide a default/empty structure if not found
        return {
            "tenant_id": tenant_id,
            "color_map": {},
            "size_groups": {},
            "category_size_map": {}
        }
    return metadata

def _get_reverse_map(mapping: Dict[str, Any]) -> Dict[Any, str]:
    """Creates a reverse map from value to key."""
    return {v: k for k, v in mapping.items()}

def _hydrate_item_attributes(item: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Converts stored IDs back to human-readable strings using metadata."""
    color_map = metadata.get("color_map", {})
    reverse_color_map = _get_reverse_map(color_map)

    size_groups = metadata.get("size_groups", {})
    
    # Hydrate colors
    if "color_ids" in item and item["color_ids"]:
        item["color"] = [reverse_color_map.get(cid, str(cid)) for cid in item["color_ids"]]
    elif "color" not in item: # Ensure 'color' key exists even if no IDs
        item["color"] = []

    # Hydrate sizes
    if "size_ids" in item and item["size_ids"]:
        # Determine which size group to use based on category
        category = item.get("category")
        size_group_name = metadata.get("category_size_map", {}).get(category, "alpha") # Default to alpha
        
        current_size_map = size_groups.get(size_group_name, {})
        reverse_size_map = _get_reverse_map(current_size_map)
        
        item["size"] = [reverse_size_map.get(sid, str(sid)) for sid in item["size_ids"]]
    elif "size" not in item: # Ensure 'size' key exists even if no IDs
        item["size"] = []
        
    return item

def _prepare_item(tenant_id: str, item_data: Dict[str, Any]) -> Dict[str, Any]:
    """Helper to structure the document with all required fields."""
    # Note: For this diff, we're allowing both string lists and ID lists.
    # In a full implementation, item_data would be pre-processed to contain only IDs.
    return {
        "tenant_id": tenant_id,
        "title": item_data.get("title"),
        "description": item_data.get("description"),
        "media": item_data.get("media", []),          # List of URLs
        "category": item_data.get("category"),
        "type": item_data.get("type"),                # Added product type
        "size": item_data.get("size", []),            # List of sizes (e.g., ["S", "M"]) - for backward compatibility/fallback
        "color": item_data.get("color", []),          # List of colors (e.g., ["Blue", "Black"]) - for backward compatibility/fallback
        "size_ids": item_data.get("size_ids", []),    # List of size IDs (e.g., [1, 2])
        "color_ids": item_data.get("color_ids", []),  # List of color IDs (e.g., [1, 3])
        "size": item_data.get("size", []),            # List of sizes (e.g., ["S", "M"])
        "color": item_data.get("color", []),          # List of colors (e.g., ["Blue", "Blsck"])
        "aize": item_data.get("size", []),            # List of sizes (e.g., ["S", "M"])
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
    
    metadata = await _get_inventory_metadata(tenant_id) # Fetch metadata once
    for item in items:
        
        item["_id"] = str(item["_id"])
        _hydrate_item_attributes(item, metadata) # Hydrate attributes
        
    return items

async def search_tenant_inventory(
    tenant_id: str, 
    category: Optional[str] = None, 
    item_type: Optional[str] = None,
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
    if item_type:
        filter_doc["type"] = item_type
    if colors:
        metadata = await _get_inventory_metadata(tenant_id)
        color_map = metadata.get("color_map", {})
        color_ids_to_search = [color_map.get(c.lower()) for c in colors if color_map.get(c.lower()) is not None]
        
        color_filters = []
        if colors:
            color_filters.append({"color": {"$in": colors}})
        if color_ids_to_search:
            color_filters.append({"color_ids": {"$in": color_ids_to_search}})
        
        if color_filters:
            # If $or already exists, append to it, otherwise create it
            if "$or" in filter_doc:
                filter_doc["$or"].append({"$or": color_filters})
            else:
                filter_doc["$or"] = color_filters

    # If $or already exists, append to it, otherwise create it
    if query and "$or" in filter_doc:
        fi
        filter_doc["color"] = {"$in": colors}lter_doc["$or"].extend([
            {"title": {"$regex": query, "$options": "i"}},
            {"description": {"$regex": query, "$options": "i"}}
        ])
        filter_doc["color"] = {"$in": colors}
    if query:
        filter_doc["$or"] = [
            {"title": {"$regex": query, "$options": "i"}},
            {"description": {"$regex": query, "$options": "i"}}
        ]

    cursor = collection.find(filter_doc).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    
    metadata = await _get_inventory_metadata(tenant_id) # Fetch metadata once
    return await cursor.to_list(length=limit)
    for item in items:
        item["_id"] = str(item["_id"])
        _hydrate_item_attributes(item, metadata) # Hydrate attributes
    return items
    return await cursor.to_list(length=limit)

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
    # These fields are now expected to be hydrated strings
    if item.get('color'):
        info += f"🎨 Colors: {', '.join(item.get('color', []))}\}\n"
        info += f"📝 {desc[:75]n..."
    if item.get('size'):
        info += f"📏 Sizes: {', '.join(item.get('size', []))}\n"
    if item.get('description'):
        desc = item.get('description')
        info += f"📝 {desc[:75]}{'...' if len(desc) > 75 else ''}\n"
        info += f"📝 {desc[:75]}...\n"
    return info

async def get_distinct_manual_categories(tenant_id: str) -> List[str]:
    """Retrieves distinct categories from the manual inventory."""
    db = get_db()
    collection = db[f"inventory.{tenant_id}"]
    categories = await collection.distinct("category")
    return [cat for cat in categories if cat] # Filter out None or empty strings