"""Prompt package builder for advocate-grade Dava drafting."""
from __future__ import annotations
from typing import Any

class DavaComposer:
    def plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_type": "dava_plaint",
            "style": "advocate_grade_continuous_numbered_pleading",
            "numbering_rule": "Renderer assigns continuous 1 (एक), 2 (दो), ...; model never numbers paragraphs.",
            "required_facts_present": bool(facts),
        }

    def build_prompt_package(self, facts: dict, retrieval_context: str, structure: dict) -> dict:
        return {
            "system_rules": [
                "Draft a genuine Indian civil Dava/Plaint, not a summary, report, questionnaire, or template filled with labels.",
                "The structure_plan is authoritative: produce exactly one substantive pleading paragraph for each approved plan item, in the same order.",
                "The renderer adds numbering. Never put paragraph numbers into paragraph text.",
                "Do not turn routine party identity into numbered averments; parties belong in the party block.",
                "Do not expose generic metadata headings such as वादी का परिचय, प्रतिवादी का परिचय, विवादित संपत्ति, or वाद के तथ्य.",
                "Use natural advocate-style Hindi, normally beginning substantive averments with 'यह कि'.",
                "Group related facts naturally; do not create one paragraph for every input sentence.",
                "Use only explicit case facts. Never invent ownership, title, dates, rights, possession, events, witnesses, documents, statutes, limitation, valuation, court fee, jurisdiction or relief.",
                "Possession is not ownership. Cultivation is not automatically title. Attempt is not completion. Threat is not dispossession. Apprehension is not an event.",
                "Preserve every material factual modality and chronology.",
                "A requested relief belongs in prayer and must never appear as a past fact.",
                "Cause of action and jurisdiction averments must add their distinct legal pleading purpose and must not merely repeat earlier sentences.",
                "Only include limitation/valuation/court-fee material when the structure plan and supplied facts support it.",
                "Prayer items must be supported by supplied reliefs; do not invent substantive remedies.",
                "Use clean standard Unicode Hindi. Never copy corrupted legacy encoding.",
                "Use the advocate identity in signature_block only if supplied by the application configuration; otherwise do not invent one.",
            ],
            "facts": facts,
            "structure_plan": structure,
            "retrieved_reference_context": retrieval_context,
        }
