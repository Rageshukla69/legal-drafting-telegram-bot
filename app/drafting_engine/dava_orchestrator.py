"""Application layer for Telegram text -> Dava drafting."""

from __future__ import annotations

from pathlib import Path

from .azure_client import draft_with_azure
from .azure_prompt_builder import build_messages
from .conversation_state import CaseState, missing_fields
from .dava_composer import DavaComposer
from .question_generator import questions_for_state
from .retriever import DavaRetriever

ROOT = Path(__file__).resolve().parent


class DavaOrchestrator:
    def __init__(self) -> None:
        self.retriever = DavaRetriever(ROOT / "blueprints")
        self.composer = DavaComposer()

    def collect(self, state: CaseState, new_facts: dict) -> dict:
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

    def prepare_azure_request(self, state: CaseState) -> dict:
        if missing_fields(state.facts):
            raise ValueError("Required facts are still missing.")

        plan = self.composer.plan(state.facts)

        fact_items = state.facts.get("facts", [])
        fact_text = " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in fact_items
        )

        relief_items = state.facts.get("reliefs", [])
        relief_text = " ".join(map(str, relief_items))

        query_parts = [
            str(state.facts.get("court_name", "")),
            str(state.facts.get("plaintiff_intro", "")),
            str(state.facts.get("cause_of_action", "")),
            fact_text,
            relief_text,
        ]

        refs = self.retriever.search(
            " ".join(query_parts),
            section="dava",
            top_k=10,
        )
        context = self.retriever.format_context(refs)

        package = self.composer.build_prompt_package(
            state.facts,
            context,
            plan,
        )
        return build_messages(
            package,
            ROOT / "draft_schema.json",
        )

    def draft_live(self, state: CaseState) -> dict:
        request = self.prepare_azure_request(state)
        prompt_package = json.loads(request["messages"][1]["content"])
        return draft_with_azure(
            prompt_package,
            request["json_schema"],
        )


# Imported here to keep the function body above easy to read and test.
import json
