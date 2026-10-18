from __future__ import annotations
import json, logging, os, sqlite3, tempfile
from pathlib import Path
from typing import Any
from app.drafting_engine.conversation_state import CaseState

log = logging.getLogger("legal-bot.store")
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cases.sqlite3"


class CaseStore:
    """Cosmos-backed case state & commercial user quota store with resilient SQLite/in-memory fallback."""

    def __init__(self, path: str | Path | None = None):
        self.use_cosmos = bool(os.getenv("COSMOS_ENDPOINT") and os.getenv("COSMOS_KEY"))
        self._memory_cache: dict[str, str] = {}
        self._memory_auth: dict[str, dict[str, Any]] = {}

        if not self.use_cosmos:
            target_path = Path(path or os.getenv("SQLITE_PATH", str(DEFAULT_DB_PATH)))
            self.path = self._init_sqlite(target_path)
        else:
            try:
                from azure.cosmos import CosmosClient, PartitionKey
                self.client = CosmosClient(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"])
                self.database = self.client.create_database_if_not_exists(id=os.getenv("COSMOS_DATABASE", "legal_drafting_bot"))
                self.container = self.database.create_container_if_not_exists(
                    id=os.getenv("COSMOS_CONTAINER", "cases"),
                    partition_key=PartitionKey(path="/user_id"),
                    offer_throughput=int(os.getenv("COSMOS_THROUGHPUT", "400")),
                )
            except Exception as e:
                log.warning("Cosmos DB initialization failed (%s); falling back to resilient SQLite.", e)
                self.use_cosmos = False
                target_path = Path(path or os.getenv("SQLITE_PATH", str(DEFAULT_DB_PATH)))
                self.path = self._init_sqlite(target_path)

    def _init_sqlite(self, target_path: Path) -> Path:
        candidates = [
            target_path,
            Path(tempfile.gettempdir()) / "legal_cases.sqlite3",
            Path("/tmp/legal_cases.sqlite3"),
        ]
        last_err: Exception | None = None
        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(candidate, timeout=10.0) as db:
                    db.execute("PRAGMA journal_mode=WAL;")
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS cases ("
                        "case_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, state_json TEXT NOT NULL"
                        ")"
                    )
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS authorized_users ("
                        "user_id TEXT PRIMARY KEY, "
                        "added_by TEXT NOT NULL, "
                        "plan TEXT NOT NULL DEFAULT 'unlimited', "
                        "quota_limit INTEGER NOT NULL DEFAULT -1, "
                        "used_documents INTEGER NOT NULL DEFAULT 0, "
                        "authorized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                        ")"
                    )
                    # Auto-migrate existing SQLite tables that were created before commercial quota columns existed
                    existing_cols = {row[1] for row in db.execute("PRAGMA table_info(authorized_users)").fetchall()}
                    if "plan" not in existing_cols:
                        db.execute("ALTER TABLE authorized_users ADD COLUMN plan TEXT NOT NULL DEFAULT 'unlimited'")
                    if "quota_limit" not in existing_cols:
                        db.execute("ALTER TABLE authorized_users ADD COLUMN quota_limit INTEGER NOT NULL DEFAULT -1")
                    if "used_documents" not in existing_cols:
                        db.execute("ALTER TABLE authorized_users ADD COLUMN used_documents INTEGER NOT NULL DEFAULT 0")
                    db.commit()
                log.info("SQLite initialized at %s", candidate)
                return candidate
            except (sqlite3.OperationalError, OSError) as exc:
                last_err = exc
                log.warning("Could not initialize SQLite at %s (%s); trying fallback path...", candidate, exc)
                continue

        log.warning("All on-disk SQLite paths failed (%s); using in-memory SQLite store.", last_err)
        return Path(":memory:")

    def _get_db(self):
        return sqlite3.connect(self.path, timeout=10.0)

    def get(self, case_id: str) -> CaseState | None:
        if self.use_cosmos:
            user_id = case_id.split("-")[1] if case_id.startswith("tg-") else "unknown"
            try:
                item = self.container.read_item(item=case_id, partition_key=user_id)
                return CaseState.from_dict(item["state"])
            except Exception:
                pass

        if str(self.path) == ":memory:":
            raw = self._memory_cache.get(case_id)
            return CaseState.from_dict(json.loads(raw)) if raw else None

        try:
            with self._get_db() as db:
                row = db.execute("SELECT state_json FROM cases WHERE case_id=?", (case_id,)).fetchone()
            return CaseState.from_dict(json.loads(row[0])) if row else None
        except Exception as exc:
            log.warning("SQLite read failed for %s (%s); checking memory cache.", case_id, exc)
            raw = self._memory_cache.get(case_id)
            return CaseState.from_dict(json.loads(raw)) if raw else None

    def save(self, user_id: int | str, state: CaseState) -> None:
        state_dict = state.to_dict()
        state_json = json.dumps(state_dict, ensure_ascii=False)
        self._memory_cache[state.case_id] = state_json

        if self.use_cosmos:
            try:
                self.container.upsert_item({
                    "id": state.case_id,
                    "case_id": state.case_id,
                    "user_id": str(user_id),
                    "state": state_dict,
                })
                return
            except Exception as exc:
                log.warning("Cosmos save failed (%s); falling back to local SQLite.", exc)

        if str(self.path) == ":memory:":
            return

        try:
            with self._get_db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO cases(case_id, user_id, state_json) VALUES (?,?,?)",
                    (state.case_id, str(user_id), state_json),
                )
                db.commit()
        except Exception as exc:
            log.error("Failed to persist case %s to disk SQLite (%s). Preserved in memory.", state.case_id, exc)

    def get_user_auth(self, user_id: str) -> dict[str, Any] | None:
        """Return full authorization & quota record for a user, or None if not authorized."""
        user_str = str(user_id)
        if self.use_cosmos:
            try:
                item = self.container.read_item(item=f"auth:{user_str}", partition_key="__auth__")
                return {
                    "user_id": user_str,
                    "added_by": str(item.get("added_by", "")),
                    "plan": str(item.get("plan", "unlimited")),
                    "quota_limit": int(item.get("quota_limit", -1)),
                    "used_documents": int(item.get("used_documents", 0)),
                }
            except Exception:
                pass

        if str(self.path) == ":memory:":
            rec = self._memory_auth.get(user_str)
            return dict(rec) if rec else None

        try:
            with self._get_db() as db:
                row = db.execute(
                    "SELECT user_id, added_by, plan, quota_limit, used_documents FROM authorized_users WHERE user_id=?",
                    (user_str,),
                ).fetchone()
            if row:
                return {
                    "user_id": str(row[0]),
                    "added_by": str(row[1]),
                    "plan": str(row[2] or "unlimited"),
                    "quota_limit": int(row[3] if row[3] is not None else -1),
                    "used_documents": int(row[4] if row[4] is not None else 0),
                }
            return None
        except Exception:
            rec = self._memory_auth.get(user_str)
            return dict(rec) if rec else None

    def is_user_authorized(self, user_id: str) -> bool:
        return self.get_user_auth(str(user_id)) is not None

    def authorize_user(
        self,
        user_id: str,
        added_by: str,
        plan: str = "unlimited",
        quota_limit: int = -1,
        used_documents: int = 0,
    ) -> None:
        user_str = str(user_id)
        record = {
            "user_id": user_str,
            "added_by": str(added_by),
            "plan": str(plan or "unlimited"),
            "quota_limit": int(quota_limit),
            "used_documents": int(used_documents),
        }
        self._memory_auth[user_str] = record

        if self.use_cosmos:
            try:
                self.container.upsert_item({
                    "id": f"auth:{user_str}",
                    "user_id": "__auth__",
                    "record_type": "authorized_user",
                    "authorized_user_id": user_str,
                    "added_by": str(added_by),
                    "plan": record["plan"],
                    "quota_limit": record["quota_limit"],
                    "used_documents": record["used_documents"],
                })
                return
            except Exception as exc:
                log.warning("Cosmos authorization save failed (%s); saving locally.", exc)

        if str(self.path) == ":memory:":
            return

        try:
            with self._get_db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO authorized_users"
                    "(user_id, added_by, plan, quota_limit, used_documents, authorized_at) "
                    "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (user_str, str(added_by), record["plan"], record["quota_limit"], record["used_documents"]),
                )
                db.commit()
        except Exception as exc:
            log.error("SQLite authorization save failed (%s).", exc)

    def increment_user_usage(self, user_id: str) -> dict[str, Any] | None:
        """Increment `used_documents` by 1 for a user and persist the updated record."""
        rec = self.get_user_auth(str(user_id))
        if not rec:
            return None
        new_used = int(rec.get("used_documents", 0)) + 1
        self.authorize_user(
            user_id=str(user_id),
            added_by=rec.get("added_by", ""),
            plan=rec.get("plan", "unlimited"),
            quota_limit=int(rec.get("quota_limit", -1)),
            used_documents=new_used,
        )
        rec["used_documents"] = new_used
        return rec

    def unauthorize_user(self, user_id: str) -> bool:
        user_str = str(user_id)
        self._memory_auth.pop(user_str, None)

        if self.use_cosmos:
            try:
                self.container.delete_item(item=f"auth:{user_str}", partition_key="__auth__")
                return True
            except Exception:
                pass

        if str(self.path) == ":memory:":
            return True

        try:
            with self._get_db() as db:
                cur = db.execute("DELETE FROM authorized_users WHERE user_id=?", (user_str,))
                db.commit()
                return cur.rowcount > 0
        except Exception:
            return True

    def list_authorized_users(self) -> list[dict[str, Any]]:
        if self.use_cosmos:
            try:
                query = "SELECT c.authorized_user_id, c.added_by, c.plan, c.quota_limit, c.used_documents FROM c WHERE c.record_type = 'authorized_user'"
                items = list(self.container.query_items(query=query, enable_cross_partition_query=True))
                return [
                    {
                        "user_id": str(i.get("authorized_user_id")),
                        "added_by": str(i.get("added_by", "")),
                        "plan": str(i.get("plan", "unlimited")),
                        "quota_limit": int(i.get("quota_limit", -1) if i.get("quota_limit") is not None else -1),
                        "used_documents": int(i.get("used_documents", 0) or 0),
                    }
                    for i in items
                ]
            except Exception:
                pass

        if str(self.path) == ":memory:":
            return list(self._memory_auth.values())

        try:
            with self._get_db() as db:
                rows = db.execute(
                    "SELECT user_id, added_by, plan, quota_limit, used_documents FROM authorized_users ORDER BY user_id"
                ).fetchall()
            return [
                {
                    "user_id": str(uid),
                    "added_by": str(added_by),
                    "plan": str(plan or "unlimited"),
                    "quota_limit": int(qlim if qlim is not None else -1),
                    "used_documents": int(used if used is not None else 0),
                }
                for uid, added_by, plan, qlim, used in rows
            ]
        except Exception:
            return list(self._memory_auth.values())

    def delete(self, case_id: str) -> None:
        self._memory_cache.pop(case_id, None)
        if self.use_cosmos:
            user_id = case_id.split("-")[1] if case_id.startswith("tg-") else "unknown"
            try:
                self.container.delete_item(item=case_id, partition_key=user_id)
            except Exception:
                pass
            return
        if str(self.path) == ":memory:":
            return
        try:
            with self._get_db() as db:
                db.execute("DELETE FROM cases WHERE case_id=?", (case_id,))
                db.commit()
        except Exception:
            pass
