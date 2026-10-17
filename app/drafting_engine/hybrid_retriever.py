from __future__ import annotations
import json, math, os, re
from collections import Counter
from pathlib import Path
from typing import Any
from .hindi_corpus_guard import is_clean_hindi_reference, safe_reference_block
from .krutidev_converter import decode_krutidev_hybrid

TOKEN_RE = re.compile(r'[A-Za-z0-9\u0900-\u097F]+', re.UNICODE)


def toks(t: str) -> list[str]:
    return [x.casefold() for x in TOKEN_RE.findall(t or '') if len(x) > 1]


def tokset(t: str) -> set[str]:
    return set(toks(t))


def _is_dad_document(d: dict[str, Any]) -> bool:
    src = str(d.get("source", "")).lower()
    if src == "dad_corpus":
        return True
    if src == "professional_template":
        return False
    fn = str(d.get("filename", "")).lower()
    return "b.d. shukla" in fn or "b.d shukla" in fn or "bd shukla" in fn or "v.d. shukla" in fn


class HybridCorpusRetriever:
    """Multi-Mode Corpus Retriever:
    Supports:
    1. `template_mode`: Strictly selects the best-suited template from the 500+ purchased legal templates.
    2. `dad_style`: Strictly selects the best-suited reference from Advocate B.D. Shukla's 306 court files.
    3. `hybrid_mode`: Combines both sources.
    """

    def __init__(self, index_path=None):
        default = Path(__file__).resolve().parents[2] / 'corpus_index.json'
        self.path = Path(index_path or os.getenv('CORPUS_INDEX_PATH', str(default)))
        if not self.path.exists():
            raise FileNotFoundError(f'Corpus index not found at {self.path}. Upload corpus_index.json to the repository root.')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.documents = list(data.get('documents', []))
        if not self.documents:
            raise ValueError('corpus_index.json contains no documents.')

        for d in self.documents:
            if "source" not in d:
                d["source"] = "dad_corpus" if _is_dad_document(d) else "professional_template"

        self.df = Counter()
        self.doc_tokens = []
        self.n = max(1, len(self.documents))
        for d in self.documents:
            text = ' '.join([
                str(d.get('filename', '')),
                str(d.get('category', '')),
                str(d.get('document_type', '')),
                str(d.get('source', '')),
                str(d.get('text', '')),
            ])
            ts = tokset(text)
            self.doc_tokens.append(ts)
            for t in ts:
                self.df[t] += 1

    def _score_candidates(self, q: set[str], candidates: list[tuple[int, dict[str, Any]]]) -> list[tuple[float, dict[str, Any]]]:
        scored = []
        for orig_idx, d in candidates:
            overlap = q & self.doc_tokens[orig_idx]
            score = sum(math.log((1 + self.n) / (1 + self.df.get(t, 0))) + 1 for t in overlap)
            st = d.get('structure') or {}
            score += 0.40 if st.get('contains_prayer') else 0
            score += 0.25 if st.get('contains_verification') else 0
            score += 0.20 if st.get('contains_cause_of_action') else 0
            score += 0.15 if st.get('contains_valuation') else 0
            score += 0.10 if st.get('contains_banam') else 0
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda x: (-x[0], str(x[1].get('filename', ''))))
        return scored

    def dual_search(
        self,
        query: str,
        document_type: str = 'dava',
        top_templates: int = 2,
        top_dad: int = 2,
    ) -> dict[str, list[dict[str, Any]]]:
        """Search dual sources: professional templates AND advocate dad drafts."""
        q = tokset(decode_krutidev_hybrid(query or ''))
        if not q:
            q = tokset(document_type)

        template_candidates = []
        dad_candidates = []

        for i, d in enumerate(self.documents):
            dt = d.get('document_type')
            if document_type and dt and dt != document_type:
                continue
            if _is_dad_document(d):
                dad_candidates.append((i, d))
            else:
                template_candidates.append((i, d))

        scored_tpl = self._score_candidates(q, template_candidates)
        scored_dad = self._score_candidates(q, dad_candidates)

        if not scored_tpl:
            all_tpl = [(i, d) for i, d in enumerate(self.documents) if not _is_dad_document(d)]
            scored_tpl = self._score_candidates(q, all_tpl)
        if not scored_dad:
            all_dad = [(i, d) for i, d in enumerate(self.documents) if _is_dad_document(d)]
            scored_dad = self._score_candidates(q, all_dad)

        def _fmt(scored_items, limit):
            return [
                {
                    **{k: d.get(k) for k in ('id', 'source', 'category', 'document_type', 'filename', 'relative_path', 'text', 'structure')},
                    'score': round(s, 4),
                }
                for s, d in scored_items[:limit]
            ]

        return {
            "templates": _fmt(scored_tpl, top_templates),
            "dad_drafts": _fmt(scored_dad, top_dad),
        }

    def retrieve_for_style(
        self,
        query: str,
        document_type: str = 'dava',
        style_mode: str = 'template_mode',
    ) -> tuple[str, str]:
        """Retrieve context and selected template filename based on the user's chosen style_mode:
        - `template_mode`: Strictly uses the 500+ purchased legal templates (Template Fill-In Mode).
        - `dad_style`: Strictly uses Advocate B.D. Shukla's 306 original files.
        - `hybrid_mode`: Uses both.
        """
        mode = (style_mode or "template_mode").strip().lower()
        dual = self.dual_search(query, document_type=document_type, top_templates=3, top_dad=3)
        templates = dual.get("templates", [])
        dad_drafts = dual.get("dad_drafts", [])

        if mode == "template_mode":
            if not templates:
                templates = dad_drafts[:2]
            top_name = str(templates[0].get("filename", "Standard Legal Template")) if templates else "Standard Legal Template"
            blocks = [
                "================================================================================\n"
                "SELECTED MASTER TEMPLATE (FROM PURCHASED 500+ LEGAL TEMPLATES)\n"
                f"PRIMARY MATCHED TEMPLATE: {top_name}\n"
                "MODE: STRICT TEMPLATE FILL-IN & ADAPTATION\n"
                "CRITICAL INSTRUCTIONS:\n"
                "1. Use [MASTER TEMPLATE 1] below as your direct structural & drafting foundation.\n"
                "2. Keep its exact legal structure, headings, statutory provisions, legal clauses,\n"
                "   cause of action format, valuation/court-fee wording, and prayer structure.\n"
                "3. Replace all placeholder blanks (...) and sample details with the user's actual\n"
                "   case details from CASE_FACTS (Court Name, Court Complex, Parties' Names, Issue/Facts,\n"
                "   Property Description, Dates, Reliefs, and Advocate Name).\n"
                "4. If CASE_FACTS includes custom_add_points, integrate those points cleanly.\n"
                "5. If CASE_FACTS includes custom_remove_points, omit those points from the draft.\n"
                "================================================================================"
            ]
            for i, r in enumerate(templates[:2], 1):
                raw_text = str(r.get("text", "") or "")
                text = raw_text if len(raw_text) <= 18000 else raw_text[:18000] + "\n[END OF TEMPLATE]"
                blocks.append(
                    f"[MASTER TEMPLATE {i}]\n"
                    f"filename: {r.get('filename', '')}\n"
                    f"document_type: {r.get('document_type', '')}\n"
                    f"statutes: {', '.join(r.get('structure', {}).get('detected_statutes', []))}\n\n"
                    f"{text}"
                )
            return "\n\n".join(blocks), top_name

        if mode == "dad_style":
            if not dad_drafts:
                dad_drafts = templates[:2]
            top_name = str(dad_drafts[0].get("filename", "Advocate B.D. Shukla Draft")) if dad_drafts else "Advocate B.D. Shukla Draft"
            blocks = [
                "================================================================================\n"
                "SELECTED ORIGINAL ADVOCATE DRAFT (FROM DAD'S 306 COURT FILES)\n"
                f"PRIMARY MATCHED FILE: {top_name}\n"
                "MODE: DAD'S AUTHENTIC LEGAL STYLE & STRUCTURE REPLICATION\n"
                "CRITICAL INSTRUCTIONS:\n"
                "1. Replicate Advocate B.D. Shukla's exact legal drafting style, vocabulary\n"
                "   ('मालिक काबिज दखील', 'मुकद्दमा हाजा के वाकयात', 'शराफतन'), paragraph rhythm ('यह कि ...'),\n"
                "   party formatting, prayer phrasing, and verification wording from [DAD ORIGINAL DRAFT 1].\n"
                "2. Replace all historical case details (old party names, old dates, old gata numbers)\n"
                "   with the current case details from CASE_FACTS.\n"
                "3. If CASE_FACTS includes custom_add_points, add those points in the exact same style.\n"
                "4. If CASE_FACTS includes custom_remove_points, omit those points.\n"
                "================================================================================"
            ]
            for i, r in enumerate(dad_drafts[:3], 1):
                raw_text = str(r.get("text", "") or "")
                text = raw_text if len(raw_text) <= 15000 else raw_text[:15000] + "\n[END OF DRAFT]"
                blocks.append(
                    f"[DAD ORIGINAL DRAFT {i}]\n"
                    f"filename: {r.get('filename', '')}\n"
                    f"category: {r.get('category', '')}\n\n"
                    f"{text}"
                )
            return "\n\n".join(blocks), top_name

        # Default / hybrid_mode
        tpl_name = str(templates[0].get("filename", "")) if templates else ""
        dad_name = str(dad_drafts[0].get("filename", "")) if dad_drafts else ""
        combined_name = f"{tpl_name} + {dad_name}".strip(" +")
        return self.format_dual_context(dual), combined_name

    def search(self, query: str, document_type: str = 'dava', top_k: int | None = None) -> list[dict[str, Any]]:
        """Standard search with balanced source representation."""
        top_k = top_k or int(os.getenv('CORPUS_TOP_K', '8'))
        dual = self.dual_search(query, document_type=document_type, top_templates=max(1, top_k // 2), top_dad=max(1, top_k // 2))
        combined = []
        seen_ids = set()

        tpls = dual.get("templates", [])
        dads = dual.get("dad_drafts", [])
        for tpl, dad in zip(tpls, dads):
            if tpl["id"] not in seen_ids:
                combined.append(tpl)
                seen_ids.add(tpl["id"])
            if dad["id"] not in seen_ids:
                combined.append(dad)
                seen_ids.add(dad["id"])

        for rem in (tpls + dads):
            if rem["id"] not in seen_ids:
                combined.append(rem)
                seen_ids.add(rem["id"])

        if len(combined) < top_k:
            q = tokset(decode_krutidev_hybrid(query or ''))
            all_cands = [(i, d) for i, d in enumerate(self.documents) if (not document_type or d.get('document_type') == document_type)]
            for s, d in self._score_candidates(q, all_cands):
                if d.get("id") not in seen_ids:
                    combined.append({
                        **{k: d.get(k) for k in ('id', 'source', 'category', 'document_type', 'filename', 'relative_path', 'text', 'structure')},
                        'score': round(s, 4),
                    })
                    seen_ids.add(d.get("id"))
                if len(combined) >= top_k:
                    break

        return combined[:top_k]

    def format_dual_context(self, dual_results: dict[str, list[dict[str, Any]]], max_chars: int | None = None) -> str:
        """Format retrieved templates and dad drafts into two distinctly prioritized sections."""
        max_chars = max_chars or int(os.getenv('CORPUS_CONTEXT_MAX_CHARS', '75000'))
        blocks = []
        used = 0

        templates = dual_results.get("templates", [])
        dad_drafts = dual_results.get("dad_drafts", [])

        if templates:
            blocks.append(
                "================================================================================\n"
                "SECTION 1: PRIMARY PROFESSIONAL LEGAL TEMPLATE (STRUCTURAL BLUEPRINT)\n"
                "PRIORITY: HIGHEST FOR LEGAL STRUCTURE & STATUTORY PRECISION.\n"
                "INSTRUCTION: Use this template for statutory provisions (CPC/CrPC/Specific Relief Act/NI Act),\n"
                "order/rule headings, standard cause of action format, jurisdiction, valuation, and prayer clauses.\n"
                "================================================================================"
            )
            for i, r in enumerate(templates, 1):
                raw_text = str(r.get('text', '') or '')
                text = raw_text if len(raw_text) <= 12000 else raw_text[:12000] + '\n[END OF EXCERPT]'
                block = (
                    f"[PRIMARY PROFESSIONAL TEMPLATE {i}]\n"
                    f"filename: {r.get('filename', '')}\n"
                    f"category: {r.get('category', '')}\n"
                    f"statutes: {', '.join(r.get('structure', {}).get('detected_statutes', []))}\n"
                    f"ROLE: PRIMARY STRUCTURAL BLUEPRINT & PROCEDURAL FRAMEWORK.\n\n"
                    f"{text}"
                )
                if used + len(block) > max_chars:
                    break
                blocks.append(block)
                used += len(block)

        if dad_drafts:
            blocks.append(
                "================================================================================\n"
                "SECTION 2: ADVOCATE B.D. SHUKLA'S COURT DRAFTS (LANGUAGE, TONE & VOCABULARY)\n"
                "PRIORITY: HIGHEST FOR AUTHENTIC HINDI ADVOCACY PHRASING & VERNACULAR TONE.\n"
                "INSTRUCTION: Infuse Advocate B.D. Shukla's authentic court vocabulary ('मालिक काबिज दखील',\n"
                "'मुकद्दमा हाजा के वाकयात', 'शराफतन'), transition rhythm ('यह कि ...'), and respectful tone.\n"
                "DO NOT copy case-specific parties, dates, plot numbers, or past facts from these examples.\n"
                "================================================================================"
            )
            for i, r in enumerate(dad_drafts, 1):
                raw_text = str(r.get('text', '') or '')
                text = raw_text if len(raw_text) <= 12000 else raw_text[:12000] + '\n[END OF EXCERPT]'
                block = (
                    f"[ADVOCATE B.D. SHUKLA DRAFT {i}]\n"
                    f"filename: {r.get('filename', '')}\n"
                    f"category: {r.get('category', '')}\n"
                    f"ROLE: VERNACULAR ADVOCACY STYLE, TRANSITIONS & AUTHENTIC COURT PHRASING.\n\n"
                    f"{text}"
                )
                if used + len(block) > max_chars:
                    break
                blocks.append(block)
                used += len(block)

        return '\n\n'.join(blocks)

    def format_context(self, results: list[dict[str, Any]], max_chars: int | None = None) -> str:
        """Backward-compatible formatting for a unified list of results."""
        max_chars = max_chars or int(os.getenv('CORPUS_CONTEXT_MAX_CHARS', '60000'))
        blocks = []
        used = 0
        include_legacy_text = os.getenv('CORPUS_ALLOW_LEGACY_TEXT', 'false').strip().lower() == 'true'
        for i, r in enumerate(results, 1):
            raw_text = str(r.get('text', '') or '')
            is_dad = _is_dad_document(r)
            role_label = (
                "ADVOCATE B.D. SHUKLA DRAFT — STYLE, TERMINOLOGY & VERNACULAR PHRASING REFERENCE"
                if is_dad
                else "PRIMARY PROFESSIONAL LEGAL TEMPLATE — STRUCTURAL & STATUTORY BLUEPRINT"
            )
            if include_legacy_text or is_clean_hindi_reference(raw_text):
                text = raw_text if len(raw_text) <= 14000 else raw_text[:14000] + '\n[END OF EXCERPT]'
                block = (
                    f"[REFERENCE {i} — {role_label}]\n"
                    f"filename: {r.get('filename', '')}\n"
                    f"source: {r.get('source', '')}\n"
                    f"category: {r.get('category', '')}\n"
                    f"retrieval_score: {r.get('score', 0)}\n"
                    f"{text}"
                )
            else:
                block = safe_reference_block(r, i)
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return '\n\n'.join(blocks)
