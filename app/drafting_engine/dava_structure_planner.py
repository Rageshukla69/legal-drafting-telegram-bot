"""Two-pass Dava planning: retrieve first, then Gemini structure analysis."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient
from .hybrid_retriever import AdvocateCorpusRetriever

ROOT = Path(__file__).resolve().parent

class DavaStructurePlanner:
    def __init__(self, retriever: AdvocateCorpusRetriever, client: GeminiClient | None = None):
        self.retriever = retriever
        self.client = client or GeminiClient()

    @staticmethod
    def retrieval_query(facts: dict[str, Any]) -> str:
        parts = [
            facts.get("suit_nature", ""), facts.get("property_description", ""),
            facts.get("title_right", ""), facts.get("possession", ""),
            facts.get("cause_of_action", ""), facts.get("jurisdiction_facts", ""),
            " ".join(facts.get("defendant_conduct", [])),
            " ".join(facts.get("reliefs", [])), " ".join(facts.get("prayer", [])),
        ]
        return " ".join(x for x in parts if x)

    def retrieve(self, facts: dict[str, Any], top_k: int = 8) -> list[dict[str, Any]]:
        return self.retriever.search(self.retrieval_query(facts), document_type="dava", top_k=top_k)

    def plan(self, facts: dict[str, Any], top_k: int = 8) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        hits = self.retrieve(facts, top_k=top_k)
        refs = self.retriever.compact_context(hits)
        payload = {
            "case_facts": facts,
            "retrieved_advocate_examples": refs,
            "instruction": "Reverse-engineer structure and style. Do not import any factual content from the examples."
        }
        schema = json.loads((ROOT / "schemas" / "structure_plan.json").read_text(encoding="utf-8"))
        system = (ROOT / "prompts" / "structure_analyst.txt").read_text(encoding="utf-8")
        plan = self.client.structured(system_instruction=system, payload=payload, schema=schema, temperature=0.1)
        return plan, hits
