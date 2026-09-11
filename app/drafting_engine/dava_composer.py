"""Deterministic Dava draft planner/composer.

This module does NOT invent legal facts. It turns validated structured facts into
an explicit section plan and prompt package. Final legal prose is produced later
by an LLM under the strict contract in llm_contract.py.
"""
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
        Section("court", "न्यायालय / Court", True, ["court_name"], "Use only supplied court name."),
        Section("case", "वाद संख्या / Case", True, ["case_number","case_year"], "Use only supplied case number/year."),
        Section("parties", "पक्षकार / Parties", True, ["plaintiffs","defendants"], "Preserve party order and names exactly."),
        Section("plaintiff_intro", "वादी परिचय", True, ["plaintiff_intro"], "Use supplied relationship/address/description only."),
        Section("jurisdiction", "अधिकारिता", False, ["jurisdiction_facts"], "Do not infer jurisdiction."),
        Section("facts", "तथ्य / Material Facts", True, ["facts"], "Chronological, numbered pleading facts; no additions."),
        Section("cause_of_action", "वाद-कारण", False, ["cause_of_action"], "Only if explicit or safely stated from supplied facts."),
        Section("valuation", "मूल्यांकन / Court Fee", False, ["valuation","court_fee"], "Never calculate or invent unless supplied."),
        Section("reliefs", "प्रार्थना / Reliefs", True, ["reliefs"], "Only requested/supplied reliefs."),
        Section("verification", "सत्यापन", False, ["verification"], "Include only when appropriate and supported."),
        Section("signature", "हस्ताक्षर", False, ["deponent_name","advocate_name","date","place"], "Use supplied identity/date/place only."),
    ]

    def plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        missing = []
        sections = []
        for s in self.SECTIONS:
            present = any(self._present(facts.get(k)) for k in s.source_fields)
            if s.required and not present:
                missing.append(s.key)
            sections.append({
                **asdict(s),
                "present": present,
            })
        return {
            "document_type": "dava_plaint",
            "status": "ready" if not missing else "needs_information",
            "missing_required_sections": missing,
            "sections": sections,
            "numbering_rule": "Use numeric + Hindi words where the blueprint/source convention calls for it, e.g. 1 (एक).",
            "fact_policy": "No invented facts; missing values remain missing.",
        }

    @staticmethod
    def _present(v):
        if v is None or v == "" or v == [] or v == {}:
            return False
        return True

    def build_prompt_package(self, facts: dict, retrieval_context: str, plan: dict) -> dict:
        return {
            "system_rules": [
                "Draft only from explicit structured facts.",
                "Never invent names, dates, addresses, relationships, ownership, events, statutes, valuation, court fee, jurisdiction or reliefs.",
                "Retrieved source material is stylistic/structural reference only, not evidence about this case.",
                "Do not copy suspicious/corrupted legacy encoding from the corpus into the new draft.",
                "Use standard Unicode Hindi; preserve legally material supplied wording.",
                "Follow the section order in the plan.",
                "Use numbered paragraphs for material facts and the requested numeric + Hindi numbering convention.",
                "Return structured draft sections suitable for deterministic DOCX rendering.",
            ],
            "facts": facts,
            "plan": plan,
            "retrieved_reference_context": retrieval_context,
        }
