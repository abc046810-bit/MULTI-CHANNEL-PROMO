"""MongoDB access. MongoDB is the source of truth for admins, channels, promotions and deletion schedules."""
from pymongo import ASCENDING, MongoClient


class Database:
    def __init__(self, uri: str, db_name: str) -> None:
        self.client = MongoClient(uri, serverSelectionTimeoutMS=8000, tz_aware=True)
        self.db = self.client[db_name]
        self.admins = self.db["admins"]
        self.channels = self.db["channels"]
        self.promotions = self.db["promotions"]
        self.promotion_messages = self.db["promotion_messages"]
        self.audit_logs = self.db["audit_logs"]

    def ping(self) -> None:
        self.client.admin.command("ping")

    def ensure_indexes(self) -> None:
        self.admins.create_index([("user_id", ASCENDING)], unique=True)
        self.channels.create_index([("chat_id", ASCENDING)], unique=True)
        self.promotions.create_index([("promotion_id", ASCENDING)], unique=True)
        self.promotion_messages.create_index([("status", ASCENDING), ("delete_at", ASCENDING)])
        self.promotion_messages.create_index([("promotion_id", ASCENDING), ("status", ASCENDING)])
