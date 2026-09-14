from __future__ import annotations
import json, os, sqlite3
from pathlib import Path
from app.drafting_engine.conversation_state import CaseState

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cases.sqlite3"

class CaseStore:
    """Cosmos-backed case state with SQLite fallback for local development.

    Cosmos DB for NoSQL is used when COSMOS_ENDPOINT and COSMOS_KEY are present.
    The application schema remains JSON so switching storage does not affect the
    drafting engine.
    """
    def __init__(self, path: str | Path = DB_PATH):
        self.use_cosmos = bool(os.getenv("COSMOS_ENDPOINT") and os.getenv("COSMOS_KEY"))
        self.path = Path(path)
        if not self.use_cosmos:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, state_json TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS authorized_users (user_id TEXT PRIMARY KEY, added_by TEXT NOT NULL, authorized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
                db.commit()
        else:
            from azure.cosmos import CosmosClient, PartitionKey
            self.client = CosmosClient(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"])
            self.database = self.client.create_database_if_not_exists(id=os.getenv("COSMOS_DATABASE","legal_drafting_bot"))
            self.container = self.database.create_container_if_not_exists(
                id=os.getenv("COSMOS_CONTAINER","cases"), partition_key=PartitionKey(path="/user_id"),
                offer_throughput=int(os.getenv("COSMOS_THROUGHPUT","400"))
            )

    def get(self, case_id: str) -> CaseState | None:
        if self.use_cosmos:
            # Case IDs are unique, and user_id is embedded as tg-<id>-<...>.
            user_id = case_id.split("-")[1] if case_id.startswith("tg-") else "unknown"
            try:
                item=self.container.read_item(item=case_id, partition_key=user_id)
            except Exception:
                return None
            return CaseState.from_dict(item["state"])
        with sqlite3.connect(self.path) as db:
            row=db.execute("SELECT state_json FROM cases WHERE case_id=?",(case_id,)).fetchone()
        return CaseState.from_dict(json.loads(row[0])) if row else None

    def save(self, user_id: int | str, state: CaseState) -> None:
        if self.use_cosmos:
            self.container.upsert_item({"id":state.case_id,"case_id":state.case_id,"user_id":str(user_id),"state":state.to_dict()})
            return
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO cases(case_id,user_id,state_json) VALUES (?,?,?)",(state.case_id,str(user_id),json.dumps(state.to_dict(),ensure_ascii=False)))
            db.commit()

    def is_user_authorized(self, user_id: str) -> bool:
        if self.use_cosmos:
            try:
                self.container.read_item(item=f"auth:{user_id}", partition_key="__auth__")
                return True
            except Exception:
                return False
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT 1 FROM authorized_users WHERE user_id=?", (str(user_id),)).fetchone()
        return row is not None

    def authorize_user(self, user_id: str, added_by: str) -> None:
        if self.use_cosmos:
            self.container.upsert_item({
                "id": f"auth:{user_id}",
                "user_id": "__auth__",
                "record_type": "authorized_user",
                "authorized_user_id": str(user_id),
                "added_by": str(added_by),
            })
            return
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO authorized_users(user_id,added_by,authorized_at) VALUES (?,?,CURRENT_TIMESTAMP)",
                (str(user_id), str(added_by)),
            )
            db.commit()

    def unauthorize_user(self, user_id: str) -> bool:
        if self.use_cosmos:
            try:
                self.container.delete_item(item=f"auth:{user_id}", partition_key="__auth__")
                return True
            except Exception:
                return False
        with sqlite3.connect(self.path) as db:
            cur = db.execute("DELETE FROM authorized_users WHERE user_id=?", (str(user_id),))
            db.commit()
            return cur.rowcount > 0

    def list_authorized_users(self) -> list[dict]:
        if self.use_cosmos:
            query = "SELECT c.authorized_user_id, c.added_by FROM c WHERE c.record_type = 'authorized_user'"
            items = list(self.container.query_items(query=query, enable_cross_partition_query=True))
            return [
                {"user_id": str(i.get("authorized_user_id")), "added_by": str(i.get("added_by", ""))}
                for i in items
            ]
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT user_id, added_by FROM authorized_users ORDER BY user_id").fetchall()
        return [{"user_id": str(uid), "added_by": str(added_by)} for uid, added_by in rows]

    def delete(self, case_id: str) -> None:
        if self.use_cosmos:
            user_id=case_id.split("-")[1] if case_id.startswith("tg-") else "unknown"
            try: self.container.delete_item(item=case_id, partition_key=user_id)
            except Exception: pass
            return
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM cases WHERE case_id=?",(case_id,)); db.commit()
