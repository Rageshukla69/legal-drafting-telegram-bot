"""Advocate-style Dava/Plaint planning for Phase 7."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any

@dataclass(frozen=True)
class PleadingPart:
    key: str
    title: str
    required: bool
    source_fields: tuple[str, ...]
    numbering: bool = True

class DavaComposer:
    """Plans a plaint as a pleading, not as a case-summary report.

    The model receives a fixed legal-document architecture while the renderer
    remains deterministic. Missing technical facts are never fabricated.
    """
    PARTS = [
        PleadingPart("opening", "", True, ("plaintiff_intro", "plaintiffs"), False),
        PleadingPart("pleadings", "", True, ("facts",), True),
        PleadingPart("cause_of_action", "", True, ("cause_of_action", "facts"), True),
        PleadingPart("jurisdiction", "", False, ("jurisdiction_facts",), True),
        PleadingPart("limitation", "", False, ("limitation_facts",), True),
        PleadingPart("valuation_court_fee", "", False, ("valuation", "court_fee"), True),
    ]

    def plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        missing = []
        parts = []
        for part in self.PARTS:
            present = any(self._present(facts.get(k)) for k in part.source_fields)
            if part.required and not present:
                missing.append(part.key)
            parts.append({**asdict(part), "present": present})
        return {
            "document_type": "dava_plaint",
            "style": "continuous_numbered_pleading",
            "status": "ready" if not missing else "needs_information",
            "missing_required_parts": missing,
            "parts": parts,
            "architecture": [
                "court_heading",
                "case_heading_if_supplied",
                "plaintiff_block",
                "banam",
                "defendant_block",
                "plaint_title",
                "opening_averment",
                "numbered_averments",
                "cause_of_action_averments",
                "jurisdiction_averments_if_supported",
                "limitation_averments_if_supported",
                "valuation_and_court_fee_if_supported",
                "prayer",
                "place_date",
                "plaintiff_signature",
                "advocate_block",
                "verification_if_supported",
            ],
            "numbering_rule": "Use 1 (एक), 2 (दो), 3 (तीन) for numbered averments.",
        }

    @staticmethod
    def _present(value: Any) -> bool:
        return value not in (None, "", [], {})

    def build_prompt_package(self, facts: dict, retrieval_context: str, plan: dict) -> dict:
        return {
            "system_rules": [
                "Draft a genuine Indian civil Dava/Plaint, not a case summary or intake report.",
                "Use the supplied facts as the only factual source for the case.",
                "Do not invent names, parentage, addresses, dates, survey/gata numbers, area, ownership, possession, events, threats, documents, statutes, limitation, valuation, court fee, jurisdiction or reliefs.",
                "Retrieved corpus is only a style/structure reference. Never use it to fill missing case facts.",
                "Do not reproduce legacy/corrupted Hindi encoding from retrieved examples.",
                "Use formal, natural Hindi pleading language, normally beginning factual averments with 'यह कि'.",
                "Do not create headings such as 'वादी का परिचय', 'प्रतिवादी का परिचय', 'विवादित संपत्ति' or 'वाद के तथ्य' merely to expose internal data fields.",
                "Present the parties first, then centered 'बनाम', then the plaint title.",
                "After the title, use an opening such as 'वादी निम्नलिखित निवेदन करता है:-' when appropriate.",
                "Put factual allegations into a coherent chronological sequence. Property particulars should be integrated into the relevant averment or a clearly pleaded property paragraph, not emitted as a data-card heading.",
                "Keep cause of action, jurisdiction, limitation and valuation/court-fee as pleading averments within the numbered sequence when those facts are supported.",
                "Do not duplicate a relief as a fact. Do not create a fact from the user's desired relief.",
                "Preserve event modality exactly: attempted is not completed; threatened is not occurred; apprehended is not actual.",
                "Do not add legal conclusions unsupported by the supplied facts. Where a legal averment requires an unknown fact, omit it or leave it for the user to supply.",
                "The prayer must contain only reliefs explicitly requested or clearly represented in the structured facts.",
                "Include litigation costs or other conventional relief only when supplied by the case facts or retrieved style instruction; never assume them as case facts.",
                "Use the fixed advocate block supplied by the application only; never invent an advocate identity.",
                "Use clean Unicode Hindi and preserve numeric facts accurately.",
            ],
            "facts": facts,
            "plan": plan,
            "retrieved_reference_context": retrieval_context,
        }
