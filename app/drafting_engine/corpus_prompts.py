"""Builds specialist prompts from retrieved advocate examples."""
from __future__ import annotations

SPECIALISTS = {
    "dava": "prompts/dava_specialist.txt",
    "written_statement": "prompts/written_statement_specialist.txt",
    "application": "prompts/application_specialist.txt",
    "evidence_pw_affidavit": "prompts/evidence_specialist.txt",
    "affidavit": "prompts/affidavit_specialist.txt",
    "legal_notice": "prompts/legal_notice_specialist.txt",
    "other_civil": "prompts/other_civil_specialist.txt",
}


def build_specialist_prompt(document_type: str, case_facts: dict, examples: list[dict], prompt_root) -> str:
    template_path = prompt_root / SPECIALISTS[document_type]
    template = template_path.read_text(encoding="utf-8")
    references = []
    for i, d in enumerate(examples, 1):
        references.append({
            "example": i,
            "filename": d.get("filename"),
            "structure": d.get("structure", {}),
            "excerpt": (d.get("text") or "")[:12000],
        })
    import json
    return template + "\n\nCASE FACTS:\n" + json.dumps(case_facts, ensure_ascii=False, indent=2) + \
        "\n\nRETRIEVED ADVOCATE EXAMPLES:\n" + json.dumps(references, ensure_ascii=False, indent=2)
