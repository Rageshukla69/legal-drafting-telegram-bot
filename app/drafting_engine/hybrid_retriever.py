from __future__ import annotations
import json, math, os, re
from collections import Counter
from pathlib import Path
from typing import Any
from .hindi_corpus_guard import is_clean_hindi_reference, safe_reference_block
from .krutidev_converter import decode_krutidev_hybrid

TOKEN_RE = re.compile(r'[A-Za-z0-9\u0900-\u097F]+', re.UNICODE)

def toks(t):
    return [x.casefold() for x in TOKEN_RE.findall(t or '') if len(x) > 1]

def tokset(t):
    return set(toks(t))

class HybridCorpusRetriever:
    def __init__(self, index_path=None):
        default = Path(__file__).resolve().parents[2] / 'corpus_index.json'
        self.path = Path(index_path or os.getenv('CORPUS_INDEX_PATH', str(default)))
        if not self.path.exists():
            raise FileNotFoundError(f'Corpus index not found at {self.path}. Upload corpus_index.json to the repository root.')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.documents = list(data.get('documents', []))
        if not self.documents:
            raise ValueError('corpus_index.json contains no documents.')
        self.df = Counter()
        self.doc_tokens = []
        self.n = max(1, len(self.documents))
        for d in self.documents:
            text = ' '.join([
                str(d.get('filename', '')),
                str(d.get('category', '')),
                str(d.get('document_type', '')),
                str(d.get('text', '')),
            ])
            ts = tokset(text)
            self.doc_tokens.append(ts)
            for t in ts:
                self.df[t] += 1

    def search(self, query, document_type='dava', top_k=None):
        top_k = top_k or int(os.getenv('CORPUS_TOP_K', '8'))
        q = tokset(decode_krutidev_hybrid(query or ''))
        if not q:
            return []
        scored = []
        for i, d in enumerate(self.documents):
            if document_type and d.get('document_type') and d.get('document_type') != document_type:
                continue
            overlap = q & self.doc_tokens[i]
            score = sum(math.log((1 + self.n) / (1 + self.df.get(t, 0))) + 1 for t in overlap)
            st = d.get('structure') or {}
            score += 0.35 if st.get('contains_prayer') else 0
            score += 0.2 if st.get('contains_verification') else 0
            score += 0.1 if st.get('contains_banam') else 0
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda x: (-x[0], str(x[1].get('filename', ''))))
        return [
            {
                **{k: d.get(k) for k in ('id', 'category', 'document_type', 'filename', 'relative_path', 'text', 'structure')},
                'score': round(s, 4),
            }
            for s, d in scored[:top_k]
        ]

    def format_context(self, results, max_chars=None):
        max_chars = max_chars or int(os.getenv('CORPUS_CONTEXT_MAX_CHARS', '60000'))
        blocks = []
        used = 0
        include_legacy_text = os.getenv('CORPUS_ALLOW_LEGACY_TEXT', 'false').strip().lower() == 'true'
        for i, r in enumerate(results, 1):
            raw_text = str(r.get('text', '') or '')
            # Include full body text whenever the document is clean Unicode Hindi
            # (all 306 re-indexed corpus docs are now clean Unicode) or when legacy text is explicitly allowed.
            if include_legacy_text or is_clean_hindi_reference(raw_text):
                text = raw_text if len(raw_text) <= 14000 else raw_text[:14000] + '\n[END OF EXCERPT]'
                block = (
                    f"[ORIGINAL ADVOCATE DRAFT {i}]\n"
                    f"filename: {r.get('filename', '')}\n"
                    f"category: {r.get('category', '')}\n"
                    f"retrieval_score: {r.get('score', 0)}\n"
                    "ROLE: STYLE, TERMINOLOGY & STRUCTURE REFERENCE ONLY — DO NOT COPY CASE-SPECIFIC FACTS.\n"
                    f"{text}"
                )
            else:
                block = safe_reference_block(r, i)
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return '\n\n'.join(blocks)
