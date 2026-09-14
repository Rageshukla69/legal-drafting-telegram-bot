"""Conservative natural-language intake using Gemini structured JSON.

No Azure OpenAI dependency. The extractor only records facts explicitly stated by
 the user; it never invents missing legal/factual details.
"""
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "intake_schema.json"

SYSTEM = """You are a conservative legal-document intake extractor for an Indian advocate's drafting system.
Extract ONLY facts explicitly stated in the user's message and current case state.
Never infer names, dates, addresses, relationships, ownership, title, possession,
statutes, limitation, valuation, court fee, jurisdiction, documents, allegations,
causes of action, defences or reliefs that are not explicitly supported.
Preserve Hindi wording and material event status exactly. An attempt is not completion;
a threat is not dispossession; an allegation is not an established fact.
If the user explicitly corrects a prior fact, replace the contradicted value.
Keep facts separate from requested relief/demand/defence.
Return only JSON matching the supplied schema.
"""


def extract_case_facts(message: str, current_facts: dict[str, Any] | None = None, document_type: str = "dava_plaint") -> dict[str, Any]:
    client = GeminiClient()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {"document_type": document_type, "current_case_facts": current_facts or {}, "new_user_message": message}
    return client.generate_json(
        system=SYSTEM,
        prompt=json.dumps(payload, ensure_ascii=False, indent=2),
        schema=schema,
        thinking_level=os.getenv("GEMINI_INTAKE_THINKING_LEVEL", "medium"),
    )
