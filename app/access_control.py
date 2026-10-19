from __future__ import annotations

import inspect
import os
from typing import Any

DEFAULT_TRIAL_LIMIT = int(os.getenv("DEFAULT_TRIAL_DOCUMENT_LIMIT", "5"))
DEFAULT_PAID_LIMIT = int(os.getenv("DEFAULT_PAID_DOCUMENT_LIMIT", "50"))


class AccessController:
    """Owner/Sudo + Cosmos/SQLite-backed commercial subscription & quota controller for Telegram users.

    Plans:
    - `owner`: Sudo/Owner account (`OWNER_TELEGRAM_ID`) — permanently unlimited.
    - `trial`: Free Trial user — limited to `DEFAULT_TRIAL_LIMIT` (5 documents by default).
    - `paid`: Paid subscriber — limited to a configurable Dava/document quota (e.g. 50, 100, or unlimited `-1`).
    - `unlimited`: Legacy/unlimited authorized user (`quota_limit = -1`).
    """

    AUTH_PARTITION = "__auth__"
    AUTH_PREFIX = "auth:"

    def __init__(self, store, *, allow_unset: bool = False):
        self.store = store
        self._local_quotas: dict[str, dict[str, Any]] = {}
        raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
        if raw.isdigit():
            self._owner_id: int | None = int(raw)
        elif allow_unset:
            self._owner_id = None
        else:
            raise RuntimeError("OWNER_TELEGRAM_ID must be set to the owner's numeric Telegram user ID.")

    @property
    def owner_id(self) -> int:
        if self._owner_id is not None:
            return self._owner_id
        raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
        if raw.isdigit():
            self._owner_id = int(raw)
            return self._owner_id
        raise RuntimeError("OWNER_TELEGRAM_ID must be set to the owner's numeric Telegram user ID.")

    def is_owner(self, user_id: int | str) -> bool:
        try:
            return int(user_id) == self.owner_id
        except (TypeError, ValueError, RuntimeError):
            return False

    def is_authorized(self, user_id: int | str) -> bool:
        uid = str(user_id)
        if self.is_owner(uid):
            return True
        return self.store.is_user_authorized(uid)

    def _save_auth_record(
        self,
        user_id: int | str,
        added_by: int | str,
        plan: str = "unlimited",
        quota_limit: int = -1,
        used_documents: int = 0,
    ) -> dict[str, Any]:
        uid = str(user_id)
        rec = {
            "user_id": uid,
            "added_by": str(added_by),
            "plan": str(plan),
            "quota_limit": int(quota_limit),
            "used_documents": int(used_documents),
        }
        self._local_quotas[uid] = rec
        sig = inspect.signature(self.store.authorize_user)
        if len(sig.parameters) >= 4:
            self.store.authorize_user(
                uid,
                str(added_by),
                plan=plan,
                quota_limit=int(quota_limit),
                used_documents=int(used_documents),
            )
        else:
            self.store.authorize_user(uid, str(added_by))
        return rec

    def authorize(
        self,
        user_id: int | str,
        added_by: int | str,
        plan: str = "unlimited",
        quota_limit: int = -1,
    ) -> dict[str, Any]:
        return self._save_auth_record(user_id, added_by, plan=plan, quota_limit=quota_limit, used_documents=0)

    def authorize_trial(
        self,
        user_id: int | str,
        added_by: int | str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Authorize a user on a Trial basis (default: limited to 5 documents)."""
        quota = int(limit) if (limit is not None and int(limit) > 0) else DEFAULT_TRIAL_LIMIT
        return self._save_auth_record(user_id, added_by, plan="trial", quota_limit=quota, used_documents=0)

    def authorize_paid(
        self,
        user_id: int | str,
        added_by: int | str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Authorize a user on a Paid basis with a specific Dava/document generation limit (or -1 for unlimited)."""
        if limit is None:
            quota = DEFAULT_PAID_LIMIT
        else:
            quota = int(limit)
        return self._save_auth_record(user_id, added_by, plan="paid", quota_limit=quota, used_documents=0)

    def get_user_record(self, user_id: int | str) -> dict[str, Any] | None:
        uid = str(user_id)
        if self.is_owner(uid):
            return {
                "user_id": uid,
                "added_by": "system",
                "plan": "owner",
                "quota_limit": -1,
                "used_documents": 0,
            }
        if hasattr(self.store, "get_user_auth"):
            rec = self.store.get_user_auth(uid)
            if rec:
                return rec
        if self.store.is_user_authorized(uid):
            return self._local_quotas.get(uid, {
                "user_id": uid,
                "added_by": "",
                "plan": "unlimited",
                "quota_limit": -1,
                "used_documents": 0,
            })
        return None

    def get_plan_status(self, user_id: int | str) -> dict[str, Any]:
        rec = self.get_user_record(user_id)
        if not rec:
            return {
                "authorized": False,
                "plan": "none",
                "plan_label": "🔒 Inactive / Unregistered",
                "quota_limit": 0,
                "used_documents": 0,
                "remaining": 0,
            }
        plan = str(rec.get("plan", "unlimited")).lower()
        qlim = int(rec.get("quota_limit", -1))
        used = int(rec.get("used_documents", 0))
        if plan == "owner" or self.is_owner(user_id):
            label = "👑 Chamber Admin / Sudo (Unlimited)"
            rem: int | str = "Unlimited"
        elif plan == "trial":
            label = "🎁 Complimentary Trial Pass"
            rem = max(0, qlim - used) if qlim > 0 else "Unlimited"
        elif plan == "paid":
            label = "💎 Vakil AI Pro Member"
            rem = max(0, qlim - used) if qlim > 0 else "Unlimited"
        else:
            label = "💎 Enterprise Unlimited Pass"
            rem = max(0, qlim - used) if qlim > 0 else "Unlimited"

        return {
            "authorized": True,
            "plan": plan,
            "plan_label": label,
            "quota_limit": qlim,
            "used_documents": used,
            "remaining": rem,
        }

    def can_generate(self, user_id: int | str) -> tuple[bool, str]:
        """Verify if the user is authorized AND has remaining document quota to generate a Dava/draft."""
        uid = str(user_id)
        if self.is_owner(uid):
            return True, ""
        status = self.get_plan_status(uid)
        if not status["authorized"]:
            return (
                False,
                "🏛️ *VAKIL DRAFTING AI — ACCESS RESTRICTED*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔒 आप इस bot का उपयोग करने के लिए अभी authorized नहीं हैं।\n\n"
                "🎁 *Trial Pass (5 Drafts)* या *💎 Vakil AI Pro Membership* सक्रिय कराने के लिए एडमिन को अपनी Client ID भेजें:\n"
                f"🆔 *Client ID:* `{uid}`",
            )
        qlim = int(status["quota_limit"])
        used = int(status["used_documents"])
        plan = status["plan"]
        if qlim > 0 and used >= qlim:
            if plan == "trial":
                return (
                    False,
                    f"🏛️ *VAKIL DRAFTING AI — QUOTA NOTICE*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ *Free Trial Limit Reached ({used}/{qlim} Documents)*\n\n"
                    f"आपकी 🎁 *Complimentary Trial Pass ({qlim} ड्राफ्ट)* की सीमा पूर्ण हो चुकी है।\n"
                    f"आगे वाद-पत्र (Dava), जवाब दावा एवं अन्य विधिक ड्राफ्ट तैयार करने के लिए कृपया *💎 Vakil AI Pro Plan* सक्रिय कराएं।\n\n"
                    f"🆔 *Client ID:* `{uid}`",
                )
            return (
                False,
                f"🏛️ *VAKIL DRAFTING AI — QUOTA NOTICE*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ *Paid Plan Quota Exhausted ({used}/{qlim} Documents)*\n\n"
                f"आपके 💎 *Vakil AI Pro Plan ({qlim} ड्राफ्ट)* की वर्तमान सीमा पूर्ण हो चुकी है।\n"
                f"कृपया अपना प्लान रिन्यू या अपग्रेड कराने के लिए एडमिन से संपर्क करें।\n\n"
                f"🆔 *Client ID:* `{uid}`",
            )
        return True, ""

    def consume_quota(self, user_id: int | str) -> dict[str, Any]:
        """Increment used document count by 1 after a draft is generated (Owner is exempt)."""
        uid = str(user_id)
        if self.is_owner(uid):
            return self.get_plan_status(uid)
        if hasattr(self.store, "increment_user_usage"):
            self.store.increment_user_usage(uid)
        elif uid in self._local_quotas:
            self._local_quotas[uid]["used_documents"] = int(self._local_quotas[uid].get("used_documents", 0)) + 1
        return self.get_plan_status(uid)

    def unauthorize(self, user_id: int | str) -> bool:
        uid = str(user_id)
        if self.is_owner(uid):
            return False
        self._local_quotas.pop(uid, None)
        return self.store.unauthorize_user(uid)

    def authorized_users(self) -> list[dict[str, Any]]:
        rows = self.store.list_authorized_users()
        enriched = []
        for r in rows:
            uid = str(r.get("user_id", ""))
            local = self._local_quotas.get(uid, {})
            enriched.append({
                "user_id": uid,
                "added_by": str(r.get("added_by", local.get("added_by", ""))),
                "plan": str(r.get("plan", local.get("plan", "unlimited"))),
                "quota_limit": int(r.get("quota_limit", local.get("quota_limit", -1))),
                "used_documents": int(r.get("used_documents", local.get("used_documents", 0))),
            })
        return enriched
