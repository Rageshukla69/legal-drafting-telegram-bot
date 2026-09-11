"""Application layer for AI-structured advocate-style Dava drafting."""
from __future__ import annotations
import json
from pathlib import Path

from .azure_prompt_builder import build_messages
from .case_intake import extract_case_facts
from .conversation_state import CaseState, missing_fields
from .dava_composer import DavaComposer
from .dava_structure_planner import plan_structure
from .dava_validator import validate_dava_draft
from .question_generator import questions_for_state
from .retriever import DavaRetriever
from .azure_client import draft_with_azure

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
            return {"status": "needs_information", "missing": missing,
                    "questions": questions_for_state(state.facts)}
        state.status = "ready"
        return {"status": "ready", "facts": state.facts}

    def extract_and_collect(self, state: CaseState, text: str) -> dict:
        """Compatibility entry point used by the Telegram bot for natural-language intake."""
        extracted = extract_case_facts(text, current_facts=state.facts)
        return self.collect(state, extracted)

    def _retrieve(self, facts: dict) -> str:
        fact_items = facts.get("facts", [])
        fact_text = " ".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in fact_items)
        relief_text = " ".join(map(str, facts.get("reliefs", [])))
        query = " ".join([
            str(facts.get("court_name", "")),
            str(facts.get("plaintiff_intro", "")),
            str(facts.get("property_description", "")),
            str(facts.get("cause_of_action", "")),
            fact_text,
            relief_text,
        ])
        refs = self.retriever.search(query, section="dava", top_k=10)
        return self.retriever.format_context(refs)

    def prepare_azure_request(self, state: CaseState) -> dict:
        if missing_fields(state.facts):
            raise ValueError("Required facts are still missing.")
        context = self._retrieve(state.facts)

        # AI call #2: decide the case-specific document architecture before drafting.
        structure = plan_structure(state.facts, context)
        package = self.composer.build_prompt_package(state.facts, context, structure)
        package["structure_plan"] = structure
        return build_messages(package, ROOT / "draft_schema.json")

    def draft_live(self, state: CaseState) -> dict:
        request = self.prepare_azure_request(state)
        prompt_package = json.loads(request["messages"][1]["content"])
        last_errors: list[str] = []
        for _ in range(2):
            draft = draft_with_azure(prompt_package, request["json_schema"])
            errors = validate_dava_draft(draft, state.facts, prompt_package.get("structure_plan"))
            if not errors:
                return draft
            last_errors = errors
            prompt_package["system_rules"].append(
                "A prior candidate failed deterministic validation: " + ", ".join(errors) + ". Regenerate using the exact approved structure and facts."
            )
        raise RuntimeError("Generated Dava failed safety/structure validation: " + ", ".join(last_errors))
