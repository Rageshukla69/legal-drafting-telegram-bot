from app.drafting_engine.hybrid_retriever import HybridCorpusRetriever


def test_dual_source_retrieval_balance():
    retriever = HybridCorpusRetriever()
    res = retriever.dual_search("स्थायी निषेधाज्ञा वाद पत्र", document_type="dava", top_templates=2, top_dad=2)

    assert "templates" in res
    assert "dad_drafts" in res
    assert len(res["templates"]) > 0
    assert len(res["dad_drafts"]) > 0

    tpl = res["templates"][0]
    assert tpl.get("source") == "professional_template"

    dad = res["dad_drafts"][0]
    assert dad.get("source") == "dad_corpus"

    ctx = retriever.format_dual_context(res)
    assert "SECTION 1: PRIMARY PROFESSIONAL LEGAL TEMPLATE" in ctx
    assert "SECTION 2: ADVOCATE B.D. SHUKLA'S COURT DRAFTS" in ctx


if __name__ == "__main__":
    test_dual_source_retrieval_balance()
    print("test_dual_source_corpus passed successfully!")
