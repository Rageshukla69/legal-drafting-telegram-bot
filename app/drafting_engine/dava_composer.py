"""Deterministic Dava planner and prompt package builder."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any

@dataclass
class Section:
    key: str
    title: str
    required: bool
    source_fields: list[str]
    notes: str

class DavaComposer:
    SECTIONS = [
        Section("court", "न्यायालय", True, ["court_name"], "Use only supplied court name."),
        Section("case", "वाद संख्या", False, ["case_number", "case_year"], "Include only if supplied."),
        Section("parties", "पक्षकार", True, ["plaintiffs", "defendants"], "Preserve party order and names."),
        Section("title", "वाद पत्र", True, [], "Use as the legal document title."),
        Section("plaintiff_intro", "वादी का परिचय", False, ["plaintiff_intro"], "Use only supplied identity/address facts."),
        Section("defendant_intro", "प्रतिवादी का परिचय", False, ["defendant_intro"], "Use only supplied identity/address facts."),
        Section("property", "विवादित संपत्ति", False, ["property_description"], "Include only supplied property particulars."),
        Section("jurisdiction", "अधिकारिता", False, ["jurisdiction_facts"], "Do not infer jurisdiction."),
        Section("facts", "वाद के तथ्य", True, ["facts"], "Chronological numbered facts; no additions."),
        Section("cause_of_action", "वाद-कारण", False, ["cause_of_action"], "Only explicit/supplied facts."),
        Section("limitation", "समय-सीमा", False, ["limitation_facts"], "Only if supplied; do not calculate limitation."),
        Section("valuation", "मूल्यांकन एवं न्याय शुल्क", False, ["valuation", "court_fee"], "Never calculate or invent."),
        Section("interim_reliefs", "अंतरिम प्रार्थना", False, ["interim_reliefs"], "Only requested interim reliefs."),
        Section("reliefs", "प्रार्थना", True, ["reliefs"], "Only requested reliefs."),
        Section("verification", "सत्यापन", False, ["verification"], "Only when supplied/appropriate."),
        Section("signature", "हस्ताक्षर", False, ["place", "date"], "Use supplied place/date only."),
    ]

    def plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        missing = []
        sections = []
        for s in self.SECTIONS:
            present = True if not s.source_fields and s.key == "title" else any(self._present(facts.get(k)) for k in s.source_fields)
            if s.required and not present:
                missing.append(s.key)
            sections.append({**asdict(s), "present": present})
        return {
            "document_type": "dava_plaint",
            "status": "ready" if not missing else "needs_information",
            "missing_required_sections": missing,
            "sections": sections,
            "numbering_rule": "Use numeric + Hindi words where appropriate, e.g. 1 (एक).",
            "fact_policy": "No invented facts; missing values remain missing.",
        }

    @staticmethod
    def _present(v):
        return v not in (None, "", [], {})

    def build_prompt_package(self, facts: dict, retrieval_context: str, plan: dict) -> dict:
        return {
            "system_rules": [
                "Draft a usable Indian civil Dava/Plaint from explicit structured case facts only.",
                "Never invent names, dates, addresses, relationships, ownership, survey numbers, events, statutes, valuation, court fee, limitation, jurisdiction or reliefs.",
                "Retrieved source material is style/structure reference only and is not evidence about this case.",
                "Do not copy corrupted legacy encoding from the corpus.",
                "Use clean standard Unicode Hindi and preserve legally material supplied wording.",
                "Follow the supplied section plan; omit sections whose source fields are absent.",
                "Use chronological numbered pleading paragraphs and numeric + Hindi numbering such as 1 (एक).",
                "For parties, return plaintiff entries first and defendant entries second; renderer will place deterministic 'बनाम' between them.",
                "Do not cite statutes or case law unless explicitly supplied by the user or reference instructions.",
                "Preserve the exact event status/modality from the facts: attempted is not completed, threatened is not occurred, apprehended is not actual, and requested relief is not a past event.",
                "Do not turn a drafting assistant into legal advice; output only the requested draft structure.",
            ],
            "facts": facts,
            "plan": plan,
            "retrieved_reference_context": retrieval_context,
        }
