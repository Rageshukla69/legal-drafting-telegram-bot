import json
from pathlib import Path

from app.drafting_engine.hybrid_retriever import HybridCorpusRetriever


def test_default_context_does_not_send_legacy_body(tmp_path, monkeypatch):
    index = {
        "documents": [{
            "id": "1",
            "category": "01_Vaad_Patra_Dava",
            "document_type": "dava",
            "filename": "example.docx",
            "text": "mपराsDत वादी निEनलिखित निवsदन करतs है",
            "structure": {"contains_prayer": True},
        }]
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("CORPUS_ALLOW_LEGACY_TEXT", raising=False)
    r = HybridCorpusRetriever(path)
    refs = r.search("वादी प्रार्थना", document_type="dava", top_k=1)
    context = r.format_context(refs)
    assert "mपराsDत" not in context
    assert "STRUCTURE/METADATA REFERENCE ONLY" in context
