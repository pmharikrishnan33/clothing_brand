import os
from functools import lru_cache

from pymongo import MongoClient
from app.core.config import MONGO_URI, MONGO_DB_NAME

@lru_cache(maxsize=1)
def get_mongo_client() -> MongoClient:
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured in environment variables.")
    return MongoClient(MONGO_URI)

def get_db():
    client = get_mongo_client()
    return client[MONGO_DB_NAME]
