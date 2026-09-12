"""Gemini Dava composer using a separately analyzed corpus plan."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient
from .hybrid_retriever import AdvocateCorpusRetriever

ROOT = Path(__file__).resolve().parent

class DavaComposer81:
    def __init__(self, client: GeminiClient | None = None):
        self.client = client or GeminiClient()

    def compose(self, facts: dict[str, Any], plan: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
        refs = AdvocateCorpusRetriever.compact_context(hits)
        payload = {
            "current_case_facts": facts,
            "structure_plan": plan,
            "retrieved_advocate_examples": refs,
            "output_contract": "Generate only the new Dava JSON. Current facts are authoritative; examples are style/structure references only."
        }
        schema = json.loads((ROOT / "schemas" / "dava_draft.json").read_text(encoding="utf-8"))
        system = (ROOT / "prompts" / "dava_master.txt").read_text(encoding="utf-8") + "\n\n" + (ROOT / "prompts" / "dava_composer.txt").read_text(encoding="utf-8")
        return self.client.structured(system_instruction=system, payload=payload, schema=schema, temperature=0.15)
