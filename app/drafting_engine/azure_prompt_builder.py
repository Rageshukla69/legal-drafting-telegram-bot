"""Azure Structured Outputs prompt builder for the two-stage Dava pipeline."""
from __future__ import annotations
import json
from pathlib import Path


def build_messages(prompt_package: dict, schema_path: str | Path):
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    system = """You are the final drafting layer of a conservative Indian civil-pleading system.
Write the final Dava/Plaint using ONLY the supplied facts and the approved structure_plan.

The structural planner has already decided what numbered averments this particular case needs.
You must now fill those approved slots with precise, natural, formal Hindi pleading language.

STRICT OUTPUT RULES:
- Produce a filed-pleading style document, not a summary or report.
- Write exactly one paragraph for each item in structure_plan.paragraphs, in that order.
- NEVER add paragraph numbers to paragraph text. The renderer adds numbering.
- Do not create extra numbered paragraphs.
- Do not duplicate a fact in another category merely to make the pleading longer.
- Do not expose database-style headings such as 'वादी का परिचय', 'प्रतिवादी का परिचय', 'विवादित संपत्ति', or 'वाद के तथ्य'.
- Keep the court/party/title/front matter separate from the numbered pleading.
- Use natural 'यह कि' averments where appropriate.
- Preserve chronology and factual modality exactly.
- Never change attempted possession into completed possession.
- Never change a threat into an event that actually happened.
- Never change apprehension into an actual event.
- Never turn the requested relief into a past factual allegation.
- Never invent missing legal or factual details.
- Include jurisdiction, limitation, valuation or court-fee averments only when the approved structure includes them and the facts support them.
- The prayer must be precise and supported by the facts.
- Use clean Unicode Hindi and do not copy corrupted legacy encoding.

Return ONLY JSON matching the supplied schema."""
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt_package, ensure_ascii=False, indent=2)},
        ],
        "json_schema": schema,
    }
