import os
from functools import lru_cache

from pymongo import MongoClient

from app.core.config import MONGO_URI


@lru_cache(maxsize=1)
def get_mongo_client() -> MongoClient:
    uri = MONGO_URI or os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI is not configured")
    return MongoClient(uri)


def get_db():
    client = get_mongo_client()
    db_name = os.getenv("MONGO_DB_NAME", "zyphor_backend")
    return client[db_name]
