"""Corpus download, extraction and structural indexing foundation."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

CATEGORIES = {
    "01_Vaad_Patra_Dava": "dava",
    "02_Jawab_Dava_WS": "written_statement",
    "03_Applications_PrarthanaPatra": "application",
    "04_Evidence_PW_Affidavits": "evidence_pw_affidavit",
    "05_ShapathPatra_Affidavits": "affidavit",
    "06_Legal_Notices": "legal_notice",
    "08_Other_Civil_Drafts": "other_civil",
}


def _read_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())


def _read_pdf(path: Path) -> str:
    try:
        import fitz
    except ImportError:
        return ""
    doc = fitz.open(str(path))
    return "\n".join(page.get_text("text") for page in doc)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    return ""


def structural_signals(text: str) -> dict:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    joined = "\n".join(lines)
    headings = []
    for line in lines:
        if len(line) <= 100 and (
            "प्रार्थना" in line or "सत्यापन" in line or "वादपत्र" in line
            or "शपथपत्र" in line or "आवेदन" in line or "बनाम" == line
        ):
            headings.append(line)
    numbered = re.findall(r"(?m)^\s*(\d+)(?:\s*\([^\n)]*\))?\s*[:.)-]?\s*", joined)
    return {
        "line_count": len(lines),
        "numbered_paragraph_count": len(numbered),
        "first_lines": lines[:12],
        "last_lines": lines[-12:],
        "detected_headings": headings[:30],
        "contains_banam": any(x == "बनाम" for x in lines),
        "contains_prayer": "प्रार्थना" in joined,
        "contains_verification": "सत्यापन" in joined or "सत्यापित" in joined,
        "contains_advocate": "अधिवक्ता" in joined,
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_category_zip(zip_path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(destination)
    return sorted(
        p for p in destination.rglob("*")
        if p.is_file() and p.suffix.lower() in {".docx", ".pdf"}
    )


def build_index(corpus_root: Path) -> dict:
    documents = []
    for category_folder, document_type in CATEGORIES.items():
        folder = corpus_root / category_folder
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf"}:
                continue
            text = extract_text(path)
            documents.append({
                "id": hashlib.sha1(str(path.relative_to(corpus_root)).encode()).hexdigest()[:16],
                "category": category_folder,
                "document_type": document_type,
                "filename": path.name,
                "relative_path": str(path.relative_to(corpus_root)),
                "sha256": sha256(path),
                "text": text,
                "structure": structural_signals(text),
            })
    return {
        "schema_version": "phase8.0",
        "source_rule": "category folders are ground-truth document types",
        "documents": documents,
    }


def save_index(index: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
