"""Provider-neutral Azure OpenAI prompt builder.

The actual Azure client call is intentionally left out so the package can be
tested offline. Use this package with Azure OpenAI Structured Outputs later.
"""
from __future__ import annotations
import json
from pathlib import Path

def build_messages(prompt_package: dict, schema_path: str | Path):
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    system = """You are a conservative Indian civil-pleading drafting assistant.
Produce a Dava/Plaint only from the supplied structured case facts.
The retrieved corpus is reference material for structure and drafting style only.
It is NOT evidence and must never supply missing case facts.
Never invent or infer names, dates, addresses, relationships, events, ownership,
valuation, court fee, jurisdiction, statutes or reliefs. If a value is absent,
leave it empty or omit the corresponding content.
Use clean standard Unicode Hindi and preserve supplied legally material wording.
Do not reproduce corrupted legacy encodings found in source examples.
Return only the requested structured JSON object."""
    user = json.dumps(prompt_package, ensure_ascii=False, indent=2)
    return {
        "messages":[
            {"role":"system","content":system},
            {"role":"user","content":user}
        ],
        "json_schema":schema
    }
