from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException
from app.services.inventory_service import add_inventory_item, get_inventory_by_tenant

router = APIRouter(prefix="/inventory", tags=["inventory"])

@router.post("/{tenant_id}/items")
async def create_item(tenant_id: str, item: Dict[str, Any]):
    """Endpoint to add a new clothing item for a specific client."""
    try:
        item_id = await add_inventory_item(tenant_id, item)
        return {"status": "success", "item_id": item_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{tenant_id}/items")
async def list_items(tenant_id: str):
    """Endpoint to list all clothing items for a specific client."""
    try:
        items = await get_inventory_by_tenant(tenant_id)
        return {"tenant_id": tenant_id, "count": len(items), "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))