"""Application layer for Phase 7 advocate-style Dava drafting.

Keeps the Phase 6 Telegram intake API (extract_and_collect) while adding the
Phase 7 pleading planner/validator pipeline.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from .azure_client import draft_with_azure
from .azure_prompt_builder import build_messages
from .case_intake import extract_case_facts
from .conversation_state import CaseState, missing_fields
from .dava_composer import DavaComposer
from .dava_validator import validate_dava_draft
from .question_generator import questions_for_state
from .retriever import DavaRetriever

ROOT = Path(__file__).resolve().parent


class DavaOrchestrator:
    def __init__(self) -> None:
        self.retriever = DavaRetriever(ROOT / "blueprints")
        self.composer = DavaComposer()

    def collect(self, state: CaseState, new_facts: dict[str, Any]) -> dict:
        """Merge already-structured facts and return the next intake state."""
        state.merge_facts(new_facts)
        missing = missing_fields(state.facts)
        if missing:
            state.status = "collecting"
            return {
                "status": "needs_information",
                "missing": missing,
                "questions": questions_for_state(state.facts),
            }
        state.status = "ready"
        return {"status": "ready", "facts": state.facts}

    def extract_and_collect(self, state: CaseState, message: str) -> dict:
        """Extract explicit facts from natural-language intake, then collect them.

        This compatibility method is required by app.bot.py. Phase 7 previously
        removed it accidentally, causing AttributeError during normal messages.
        """
        extracted = extract_case_facts(message, current_facts=state.facts)
        return self.collect(state, extracted)

    def prepare_azure_request(self, state: CaseState) -> dict:
        if missing_fields(state.facts):
            raise ValueError("Required facts are still missing.")

        plan = self.composer.plan(state.facts)
        fact_items = state.facts.get("facts", [])
        fact_text = " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in fact_items
        )
        relief_text = " ".join(map(str, state.facts.get("reliefs", [])))
        query = " ".join([
            str(state.facts.get("court_name", "")),
            str(state.facts.get("plaintiff_intro", "")),
            str(state.facts.get("property_description", "")),
            str(state.facts.get("cause_of_action", "")),
            fact_text,
            relief_text,
        ])
        refs = self.retriever.search(query, section="dava", top_k=10)
        context = self.retriever.format_context(refs)
        package = self.composer.build_prompt_package(state.facts, context, plan)
        return build_messages(package, ROOT / "draft_schema.json")

    def draft_live(self, state: CaseState) -> dict:
        request = self.prepare_azure_request(state)
        prompt_package = json.loads(request["messages"][1]["content"])
        last_errors: list[str] = []

        for _ in range(2):
            draft = draft_with_azure(prompt_package, request["json_schema"])
            errors = validate_dava_draft(draft, state.facts)
            if not errors:
                return draft
            last_errors = errors
            prompt_package["system_rules"].append(
                "A prior candidate failed deterministic validation: "
                + ", ".join(errors)
                + ". Regenerate without those errors."
            )

        raise RuntimeError(
            "Generated Dava failed safety/structure validation: "
            + ", ".join(last_errors)
        )
