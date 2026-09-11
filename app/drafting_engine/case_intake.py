"""Natural-language case intake using Azure structured outputs.

The extractor is deliberately conservative: it may copy facts explicitly stated
by the user into structured fields, but it must not infer missing legal facts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .azure_client import _azure_client

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "intake_schema.json"

SYSTEM = """You are a conservative legal-intake extraction assistant for an Indian civil Dava/Plaint workflow.
Extract ONLY facts explicitly stated in the user's message and supplied current case state.
Do not infer or invent names, dates, addresses, relationships, ownership, survey numbers,
jurisdiction, valuation, limitation, statutes, causes of action, documents or reliefs.
If a field is not explicitly supported, return an empty string or empty array.
Do not turn a vague statement into a precise legal conclusion.
Keep Hindi wording in clean Unicode. Preserve material user wording.
Facts should be returned as short chronological factual statements, not legal advice.
"""


def extract_case_facts(
    message: str,
    current_facts: dict[str, Any] | None = None
) -> dict[str, Any]:
    client = _azure_client()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    payload = {
        "current_case_facts": current_facts or {},
        "new_user_message": message,
    }

    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "case_intake",
                "strict": True,
                "schema": schema,
            },
        },
        temperature=0,
    )

    return json.loads(response.choices[0].message.content)
