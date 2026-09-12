"""Live Telegram drafting orchestrator: Gemini intake -> retrieval -> plan -> draft -> validate -> render."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .conversation_state import CaseState
from .fact_extractor import FactExtractor
from .gemini_client import GeminiClient
from .hybrid_retriever import AdvocateCorpusRetriever
from .dava_structure_planner import DavaStructurePlanner
from .dava_composer import DavaComposer81
from .dava_validator import DavaValidator
from .renderer import render_docx, render_pdf_from_docx

ROOT = Path(__file__).resolve().parent

class DavaOrchestrator:
    def __init__(self):
        self.client = GeminiClient()
        self.extractor = FactExtractor(self.client)
        index_path = Path(__import__('os').getenv("CORPUS_INDEX_PATH", str(ROOT / "corpus_index.json")))
        if index_path.exists():
            self.retriever = AdvocateCorpusRetriever.from_json(index_path)
        else:
            # Keep the bot bootable before the 306-file index is deployed.
            from .retriever import DavaRetriever
            self.retriever = None
        self.planner = DavaStructurePlanner(self.retriever, self.client) if self.retriever else None
        self.composer = DavaComposer81(self.client)
        self.validator = DavaValidator()

    def ingest(self, state: CaseState, user_text: str) -> dict[str, Any]:
        extracted = self.extractor.extract(user_text)
        state.merge_facts(extracted.get("facts", {}))
        state.document_type = extracted.get("document_type") or state.document_type
        state.facts["unclear_items"] = list(dict.fromkeys(state.facts.get("unclear_items", []) + extracted.get("unclear_items", [])))
        state.record("extracted", json.dumps(extracted, ensure_ascii=False))
        return extracted

    def ready(self, state: CaseState) -> tuple[bool, list[str]]:
        required = ["court_name", "plaintiffs", "defendants", "facts", "reliefs"]
        missing = [x for x in required if not state.facts.get(x)]
        return not missing, missing

    def draft_live(self, state: CaseState, output_dir: str | Path) -> dict[str, Any]:
        ok, missing = self.ready(state)
        if not ok:
            raise ValueError("Missing required facts: " + ", ".join(missing))
        if not self.planner:
            raise RuntimeError("CORPUS_INDEX_PATH is missing. Build the 306-document corpus index first.")
        plan, hits = self.planner.plan(state.facts, top_k=int(__import__('os').getenv("CORPUS_TOP_K", "8")))
        draft = self.composer.compose(state.facts, plan, hits)
        validation = self.validator.validate(draft, state.facts, hits)
        if not validation["ok"]:
            raise RuntimeError("Draft failed validation: " + " | ".join(validation["errors"]))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        base = "Dava_Draft_" + state.case_id.replace("/", "-")
        docx = render_docx(draft, output_dir / f"{base}.docx")
        pdf = None
        try:
            pdf = render_pdf_from_docx(docx, output_dir / f"{base}.pdf")
        except Exception:
            pass
        return {"draft": draft, "validation": validation, "retrieval": hits, "docx": str(docx), "pdf": str(pdf) if pdf else None}
