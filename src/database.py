"""MongoDB access. MongoDB is the source of truth for admins, channels, promotions and deletion schedules."""
from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, MongoClient


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
        self.promotions.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
        self.promotion_messages.create_index([("status", ASCENDING), ("delete_at", ASCENDING)])
        self.promotion_messages.create_index([("promotion_id", ASCENDING), ("status", ASCENDING)])
        self.audit_logs.create_index([("at", DESCENDING)])

    def stats(self) -> dict:
        """Quick dashboard numbers."""
        return {
            "channels_total": self.channels.count_documents({}),
            "channels_enabled": self.channels.count_documents({"enabled": True}),
            "admins": self.admins.count_documents({}),
            "promotions_active": self.promotions.count_documents({"status": "active"}),
            "promotions_total": self.promotions.count_documents({}),
            "messages_pending": self.promotion_messages.count_documents({"status": "pending"}),
            "messages_deleted": self.promotion_messages.count_documents({"status": "deleted"}),
            "messages_failed": self.promotion_messages.count_documents({"status": "failed"}),
        }
        
