"""Prompt package builder for the AI-structured Dava pipeline."""
from __future__ import annotations
from typing import Any


class DavaComposer:
    """Keeps drafting rules separate from the AI structural planner."""

    def plan(self, facts: dict[str, Any]) -> dict[str, Any]:
        # Kept for backwards compatibility/tests. The live pipeline uses the AI planner.
        return {
            "document_type": "dava_plaint",
            "style": "ai_structured_continuous_numbered_pleading",
            "numbering_rule": "The renderer adds 1 (एक), 2 (दो), 3 (तीन) etc. The model must never number paragraphs.",
            "required_facts_present": bool(facts),
        }

    def build_prompt_package(self, facts: dict, retrieval_context: str, structure: dict) -> dict:
        return {
            "system_rules": [
                "Draft a genuine Indian civil Dava/Plaint, not a case summary, questionnaire, or intake report.",
                "The supplied structure_plan is authoritative for the order and purpose of the numbered averments.",
                "Write exactly one final pleading paragraph for each approved structure_plan.paragraphs item, in the same order. Do not add, merge, reorder, or duplicate paragraphs.",
                "The renderer will add paragraph numbers. NEVER write paragraph numbers such as '1 (एक)', '1.', '(एक)' or similar numbering into the paragraph text.",
                "Normally begin each numbered averment naturally with 'यह कि'.",
                "Use only explicit case facts. Never invent names, parentage, addresses, dates, property particulars, rights, ownership, possession, events, documents, statutes, limitation, valuation, court fee, jurisdiction or reliefs.",
                "Retrieved advocate corpus is only style/organization reference. It is never a factual source for this case.",
                "Do not reproduce legacy/corrupted Hindi encoding from retrieved examples.",
                "Do not expose internal metadata headings such as 'वादी का परिचय', 'प्रतिवादी का परिचय', 'विवादित संपत्ति' or 'वाद के तथ्य'.",
                "Preserve factual modality exactly: an attempt remains an attempt; a threat remains a threat; apprehension remains apprehension; and no dispossession may be stated unless the facts expressly say dispossession occurred.",
                "Do not convert a requested relief into a past event or factual allegation.",
                "Avoid repetitive paragraphs. Cause of action, jurisdiction and other technical averments must have their own distinct purpose only when the structure plan includes them.",
                "The prayer must contain only reliefs supported by the case facts and structure plan.",
                "Do not assume conventional costs or other relief unless supported by the supplied facts/plan.",
                "Use the fixed advocate block supplied in the application if present; never invent an advocate identity.",
                "Use clean standard Unicode Hindi.",
            ],
            "facts": facts,
            "structure_plan": structure,
            "retrieved_reference_context": retrieval_context,
        }
