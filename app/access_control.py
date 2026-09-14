from __future__ import annotations

import os
from typing import Iterable


class AccessController:
    """Owner + Cosmos/SQLite-backed allowlist for Telegram users."""

    AUTH_PARTITION = "__auth__"
    AUTH_PREFIX = "auth:"

    def __init__(self, store):
        self.store = store
        raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
        if not raw.isdigit():
            raise RuntimeError("OWNER_TELEGRAM_ID must be set to the owner's numeric Telegram user ID.")
        self.owner_id = int(raw)

    def is_owner(self, user_id: int | str) -> bool:
        try:
            return int(user_id) == self.owner_id
        except (TypeError, ValueError):
            return False

    def is_authorized(self, user_id: int | str) -> bool:
        uid = str(user_id)
        if self.is_owner(uid):
            return True
        return self.store.is_user_authorized(uid)

    def authorize(self, user_id: int | str, added_by: int | str) -> None:
        self.store.authorize_user(str(user_id), str(added_by))

    def unauthorize(self, user_id: int | str) -> bool:
        if self.is_owner(user_id):
            return False
        return self.store.unauthorize_user(str(user_id))

    def authorized_users(self) -> list[dict]:
        return self.store.list_authorized_users()
