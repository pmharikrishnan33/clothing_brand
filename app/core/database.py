import os
from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.core.config import MONGO_URI, MONGO_DB_NAME

@lru_cache(maxsize=1)
def get_mongo_client() -> AsyncIOMotorClient:
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured in environment variables.")
    # serverSelectionTimeoutMS=5000 ensures we don't hang for 30s if the DB is down
    return AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)

async def init_db():
    client = get_mongo_client()
    try:
        await client.admin.command('ping')
        print(f"✅ MongoDB connected successfully to database: {MONGO_DB_NAME}")
    except Exception as e:
        print(f"❌ Failed to connect to MongoDB: {e}")

def get_db() -> AsyncIOMotorDatabase:
    client = get_mongo_client()
    return client[MONGO_DB_NAME]
