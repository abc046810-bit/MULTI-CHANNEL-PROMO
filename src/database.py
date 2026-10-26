"""MongoDB access. MongoDB is the source of truth for admins, channels, promotions and settings."""
from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient

DEFAULT_GUEST_WELCOME = (
    "👋 Welcome!\n\n"
    "This bot is for authorized admins only.\n"
    "If you need access, contact the bot owner."
)


class Database:
    def __init__(self, uri: str, db_name: str) -> None:
        self.client = MongoClient(uri, serverSelectionTimeoutMS=8000, tz_aware=True)
        self.db = self.client[db_name]
        self.admins = self.db["admins"]
        self.channels = self.db["channels"]
        self.promotions = self.db["promotions"]
        self.promotion_messages = self.db["promotion_messages"]
        self.audit_logs = self.db["audit_logs"]
        self.settings = self.db["settings"]

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
        self.settings.create_index([("key", ASCENDING)], unique=True)

    def get_setting(self, key: str, default: Any = None) -> Any:
        doc = self.settings.find_one({"key": key}, {"_id": 0, "value": 1})
        if doc is None:
            return default
        return doc.get("value", default)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings.update_one({"key": key}, {"$set": {"key": key, "value": value}}, upsert=True)

    def get_guest_welcome(self) -> str:
        text = self.get_setting("guest_welcome")
        if not text or not str(text).strip():
            return DEFAULT_GUEST_WELCOME
        return str(text)

    def stats(self) -> dict:
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
