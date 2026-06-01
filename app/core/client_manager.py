from typing import Any, Dict, Optional

from app.core.database import get_db


def get_client_config(phone_number_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    client = db.clients.find_one({"phone_number_id": str(phone_number_id), "is_active": {"$ne": False}})
    if not client:
        return None

    client["_id"] = str(client["_id"])
    if "tenant_id" not in client or not client["tenant_id"]:
        client["tenant_id"] = client["_id"]
    return client
