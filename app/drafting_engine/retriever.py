"""Lightweight local retrieval for the Dava corpus blueprint package.

No embeddings or API keys are required. It indexes the Dava phrase library,
clauses, and examples from the Phase 3 blueprint and returns evidence snippets
for the Composer/LLM. Retrieved text is reference material only: it must never
override explicit case facts.
"""
from __future__ import annotations
import json, math, re
from pathlib import Path
from collections import Counter, defaultdict

WORD_RE = re.compile(r"[A-Za-z0-9\u0900-\u097F]+", re.UNICODE)

def tokens(text: str):
    return [x.lower() for x in WORD_RE.findall(text or "") if len(x) > 1]

class DavaRetriever:
    def __init__(self, blueprint_dir: str | Path):
        self.root = Path(blueprint_dir)
        self.items = []
        self.df = Counter()
        self._load()
        self.N = max(1, len(self.items))
        self.idf = {t: math.log((1+self.N)/(1+n))+1 for t,n in self.df.items()}

    def _load_json(self, name):
        p = self.root / name
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _add(self, source, kind, obj, text):
        if not text or not text.strip():
            return
        ts = tokens(text)
        self.items.append({"source": source, "kind": kind, "data": obj, "text": text.strip(), "tokens": ts})
        for t in set(ts):
            self.df[t] += 1

    def _load(self):
        for name, kind in [
            ("phrase_library.json", "phrase"),
            ("clauses.json", "clause"),
            ("examples.json", "example"),
        ]:
            data = self._load_json(name)
            if data is None:
                continue
            self._walk(name, kind, data)

    def _walk(self, source, kind, obj, path=""):
        if isinstance(obj, dict):
            # Prefer meaningful leaf/value combinations.
            for k,v in obj.items():
                new_path = f"{path}.{k}" if path else str(k)
                if isinstance(v, str):
                    self._add(source, kind, {"path": new_path, "key": k}, v)
                else:
                    self._walk(source, kind, v, new_path)
        elif isinstance(obj, list):
            for i,v in enumerate(obj):
                self._walk(source, kind, v, f"{path}[{i}]")
        elif isinstance(obj, (int,float,bool)):
            self._add(source, kind, {"path": path}, str(obj))

    def search(self, query: str, section: str | None = None, top_k: int = 8):
        q = tokens(query)
        if not q:
            return []
        qset = Counter(q)
        scored = []
        for it in self.items:
            c = Counter(it["tokens"])
            score = 0.0
            for term, qtf in qset.items():
                if term in c:
                    score += self.idf.get(term, 1.0) * (1 + math.log(c[term])) * qtf
            if section and section.lower() in it["text"].lower():
                score += 0.8
            if score:
                scored.append((score, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": round(s,4), **{k:v for k,v in it.items() if k!="tokens"}} for s,it in scored[:top_k]]

    def format_context(self, results, max_chars=9000):
        blocks, used = [], 0
        for i,r in enumerate(results, 1):
            block = f"[Reference {i} | {r['kind']} | {r['source']}]\n{r['text']}"
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n".join(blocks)
