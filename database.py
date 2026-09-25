from motor.motor_asyncio import AsyncIOMotorClient
import os

MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://imaxprime:imaxprime@entiredatabase.pgyu5ay.mongodb.net/?appName=EntireDatabase"
)

client = AsyncIOMotorClient(MONGO_URI)
db = client['MegaRenamerBots']
users_collection   = db['users']
auth_collection    = db['authorised']   # Authorised user IDs
session_collection = db['sessions']     # MEGA login sessions

# ─────────────────────────────────────────────────────────
# User management
# ─────────────────────────────────────────────────────────

async def add_user(user_id: int):
    user = await users_collection.find_one({"_id": user_id})
    if not user:
        await users_collection.insert_one({
            "_id": user_id,
            "lifetime_renamed": 0,
            "daily_limit": 10000,
            "is_premium": True,
            "links_checked": 0,
            "language": "en"
        })
        return True
    return False

async def get_user(user_id: int):
    return await users_collection.find_one({"_id": user_id})

async def get_all_users():
    cursor = users_collection.find({}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]

async def update_rename_stats(user_id: int, files_renamed: int):
    await users_collection.update_one(
        {"_id": user_id},
        {"$inc": {"lifetime_renamed": files_renamed, "daily_limit": -files_renamed}}
    )

async def increment_links_checked(user_id: int):
    await users_collection.update_one(
        {"_id": user_id},
        {"$inc": {"links_checked": 1}}
    )

async def set_language(user_id: int, lang: str):
    await users_collection.update_one({"_id": user_id}, {"$set": {"language": lang}})

async def set_premium(user_id: int, value: bool):
    await users_collection.update_one({"_id": user_id}, {"$set": {"is_premium": value}})

async def reset_daily_limit(user_id: int, limit: int = 100):
    await users_collection.update_one({"_id": user_id}, {"$set": {"daily_limit": limit}})

# ─────────────────────────────────────────────────────────
# Auth management
# ─────────────────────────────────────────────────────────

async def add_auth(user_id: int):
    exists = await auth_collection.find_one({"_id": user_id})
    if not exists:
        await auth_collection.insert_one({"_id": user_id})

async def remove_auth(user_id: int):
    await auth_collection.delete_one({"_id": user_id})

async def is_authorised(user_id: int) -> bool:
    return await auth_collection.find_one({"_id": user_id}) is not None

async def get_auth_list() -> list:
    cursor = auth_collection.find({})
    return [doc["_id"] async for doc in cursor]

# ─────────────────────────────────────────────────────────
# Session management (for mega login persistence)
# ─────────────────────────────────────────────────────────

async def save_session(user_id: int, email: str):
    await session_collection.update_one(
        {"_id": user_id},
        {"$set": {"email": email}},
        upsert=True
    )

async def get_session(user_id: int):
    return await session_collection.find_one({"_id": user_id})

async def delete_session(user_id: int):
    await session_collection.delete_one({"_id": user_id})
