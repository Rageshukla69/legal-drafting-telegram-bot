"""Azure Structured Outputs message builder for Dava drafting."""
from __future__ import annotations
import json
from pathlib import Path

def build_messages(prompt_package: dict, schema_path: str | Path):
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    system = """You are a conservative Indian civil-pleading drafting assistant.
Produce a Dava/Plaint only from the supplied structured case facts and section plan.
Retrieved corpus material is reference material for drafting style and structure only;
it is NOT evidence and must never supply missing case facts.
Never invent or infer names, dates, addresses, relationships, ownership, survey numbers,
events, valuation, court fee, limitation, jurisdiction, statutes or reliefs.
If a value is absent, omit the corresponding section/content.
Use clean standard Unicode Hindi and preserve supplied legally material wording.
Do not reproduce corrupted legacy encodings found in source examples.
Keep parties in plaintiff-first then defendant order; set between_label to "बनाम".
Use concise, formal pleading language and numbered factual paragraphs.
CRITICAL FACT-FIDELITY RULE: preserve event modality and completion status exactly.
Never upgrade an attempted, threatened, proposed, alleged or apprehended act into a
completed act. Never turn a requested relief into a factual allegation. If the facts
say an attempted illegal कब्जा, draft only an attempted illegal कब्जा unless completion
is explicitly stated in the supplied facts.
Return only the requested structured JSON object."""
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt_package, ensure_ascii=False, indent=2)},
        ],
        "json_schema": schema,
    }
