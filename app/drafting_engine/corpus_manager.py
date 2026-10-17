"""Corpus download, extraction, structural indexing and dual-source tagging foundation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from .krutidev_converter import decode_krutidev_hybrid

CATEGORIES = {
    "00_Professional_Templates": "professional_template",
    "01_Vaad_Patra_Dava": "dava",
    "02_Jawab_Dava_WS": "written_statement",
    "03_Applications_PrarthanaPatra": "application",
    "04_Evidence_PW_Affidavits": "evidence_pw_affidavit",
    "05_ShapathPatra_Affidavits": "affidavit",
    "06_Legal_Notices": "legal_notice",
    "08_Other_Civil_Drafts": "other_civil",
}

_STATUTE_PATTERNS = [
    r"आदेश\s*(?:\d+|[IVXLCDM]+|[०-९]+)\s*नियम\s*(?:\d+|[IVXLCDM]+|[०-९]+)",
    r"Order\s*(?:\d+|[IVXLCDM]+)\s*Rule\s*(?:\d+|[IVXLCDM]+)",
    r"धारा\s*(?:\d+[A-Za-z]?|[०-९]+)",
    r"Section\s*(?:\d+[A-Za-z]?)",
    r"विशिष्ट अनुतोष अधिनियम",
    r"Specific Relief Act",
    r"परक्राम्य लिखत अधिनियम",
    r"Negotiable Instruments Act",
    r"सिविल प्रक्रिया संहिता",
    r"Code of Civil Procedure",
    r"CPC",
    r"CrPC",
    r"दण्ड प्रक्रिया संहिता",
]


def _read_docx(path: Path) -> str:
    from docx import Document
    try:
        doc = Document(str(path))
        raw = "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
        return decode_krutidev_hybrid(raw)
    except Exception:
        return ""


def _read_pdf(path: Path) -> str:
    try:
        import fitz
        doc = fitz.open(str(path))
        raw = "\n".join(page.get_text("text") for page in doc)
        return decode_krutidev_hybrid(raw)
    except ImportError:
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            raw = "\n".join((p.extract_text() or "") for p in reader.pages)
            return decode_krutidev_hybrid(raw)
        except Exception:
            return ""


def _read_txt(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return decode_krutidev_hybrid(raw)
    except Exception:
        return ""


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in {".txt", ".rtf", ".md"}:
        return _read_txt(path)
    return ""


def detect_document_type(filename: str, text: str, default: str = "dava") -> str:
    """Classify an unclassified template into one of the 7 core draft types."""
    combined = f"{filename} {text[:3000]}".lower()

    if any(k in combined for k in ("निषेधाज्ञा", "वाद पत्र", "वाद-पत्र", "वादपत्र", "plaint", "suit for", "dava")):
        return "dava"
    if any(k in combined for k in ("लिखित कथन", "जवाब दावा", "जवाब-दावा", "प्रतिवाद पत्र", "प्रतिवादपत्र", "written statement", "ws")):
        return "written_statement"
    if any(k in combined for k in ("प्रार्थना पत्र", "प्रार्थना-पत्र", "प्रार्थनापत्र", "आवेदन", "आदेश 39", "आदेश 7 नियम 11", "आदेश 6 नियम 17", "application", "interim")):
        return "application"
    if any(k in combined for k in ("बयान चीफ", "साक्ष्य शपथ पत्र", "साक्ष्य शपथपत्र", "pw-1", "pw1", "chief examination", "evidence affidavit")):
        return "evidence_pw_affidavit"
    if any(k in combined for k in ("शपथ पत्र", "शपथपत्र", "हलफनामा", "affidavit")):
        return "affidavit"
    if any(k in combined for k in ("कानूनी नोटिस", "विधिक नोटिस", "रजिस्टर्ड नोटिस", "legal notice", "138 ni act", "धारा 138")):
        return "legal_notice"
    return "other_civil"


def structural_signals(text: str) -> dict[str, Any]:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    joined = "\n".join(lines)
    headings = []
    for line in lines:
        if len(line) <= 120 and any(k in line for k in ("प्रार्थना", "सत्यापन", "वादपत्र", "वाद पत्र", "शपथपत्र", "आवेदन", "लिखित कथन", "बनाम")):
            headings.append(line)

    numbered = re.findall(r"(?m)^\s*(\d+)(?:\s*[-–—]?\s*\([^\n)]*\))?\s*[:.)-]?\s*", joined)

    detected_statutes = []
    for pat in _STATUTE_PATTERNS:
        matches = re.findall(pat, joined, flags=re.IGNORECASE)
        for m in matches:
            if m not in detected_statutes:
                detected_statutes.append(m)

    return {
        "line_count": len(lines),
        "numbered_paragraph_count": len(numbered),
        "first_lines": lines[:12],
        "last_lines": lines[-12:],
        "detected_headings": headings[:30],
        "contains_banam": any(x == "बनाम" or "बनाम" in x for x in lines[:15]),
        "contains_prayer": "प्रार्थना" in joined or "अनुतोष" in joined or "relief" in joined.lower(),
        "contains_verification": "सत्यापन" in joined or "सत्यापित" in joined or "verification" in joined.lower(),
        "contains_cause_of_action": "कारण वाद" in joined or "वाद हेतुक" in joined or "cause of action" in joined.lower(),
        "contains_valuation": "मूल्यांकन" in joined or "न्यायालय शुल्क" in joined or "court fee" in joined.lower(),
        "contains_jurisdiction": "क्षेत्राधिकार" in joined or "jurisdiction" in joined.lower(),
        "detected_statutes": detected_statutes[:15],
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
        if p.is_file() and p.suffix.lower() in {".docx", ".pdf", ".txt"}
    )


def is_dad_file(filename: str, parent_folder: str = "") -> bool:
    name_low = filename.lower()
    return "b.d. shukla" in name_low or "b.d shukla" in name_low or "bd shukla" in name_low or "v.d. shukla" in name_low


def build_index(corpus_root: Path, additional_templates_dir: Path | None = None) -> dict[str, Any]:
    documents = []

    # 1. Process known category folders in corpus_root
    for category_folder, doc_type in CATEGORIES.items():
        folder = corpus_root / category_folder
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf", ".txt"}:
                continue
            text = extract_text(path)
            if not text.strip():
                continue

            dad_flag = is_dad_file(path.name, category_folder)
            source_tag = "dad_corpus" if dad_flag else "professional_template"
            resolved_type = doc_type if doc_type != "professional_template" else detect_document_type(path.name, text)

            documents.append({
                "id": hashlib.sha1(str(path.relative_to(corpus_root)).encode()).hexdigest()[:16],
                "source": source_tag,
                "category": category_folder,
                "document_type": resolved_type,
                "filename": path.name,
                "relative_path": str(path.relative_to(corpus_root)),
                "sha256": sha256(path),
                "text": text,
                "structure": structural_signals(text),
            })

    # 2. Process optional additional external templates (e.g. 500+ purchased templates)
    if additional_templates_dir and additional_templates_dir.exists():
        for path in sorted(additional_templates_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf", ".txt", ".rtf"}:
                continue
            text = extract_text(path)
            if not text.strip():
                continue

            dad_flag = is_dad_file(path.name)
            source_tag = "dad_corpus" if dad_flag else "professional_template"
            resolved_type = detect_document_type(path.name, text)

            documents.append({
                "id": hashlib.sha1(str(path.name).encode()).hexdigest()[:16],
                "source": source_tag,
                "category": "purchased_hindi_templates",
                "document_type": resolved_type,
                "filename": path.name,
                "relative_path": str(path.name),
                "sha256": sha256(path),
                "text": text,
                "structure": structural_signals(text),
            })

    dad_count = sum(1 for d in documents if d.get("source") == "dad_corpus")
    tpl_count = sum(1 for d in documents if d.get("source") == "professional_template")

    return {
        "schema_version": "phase8.2-dual-source",
        "corpus_name": "Indian Legal Drafting Hybrid Corpus (Professional Templates + Advocate B.D. Shukla)",
        "document_count": len(documents),
        "source_counts": {
            "dad_corpus": dad_count,
            "professional_template": tpl_count,
        },
        "documents": documents,
    }


def save_index(index: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
