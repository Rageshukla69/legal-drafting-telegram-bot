#!/usr/bin/env python3
"""Dedicated Devanagari PDF font/shaping probe.

Renders a small PDF containing the Hindi strings that previously became empty
boxes in the ReportLab pipeline. Inspect the PDF visually AND by extraction.

This script does not touch the DOCX renderer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.drafting_engine.renderers import legal_document_renderer as ldr
from app.drafting_engine.renderers import docx_to_pdf

LINES = [
    "न्यायालय",
    "श्रीमान",
    "सिविल जज",
    "जूनियर डिवीजन",
    "बिधूना",
    "(एक)",
    "(दो)",
    "(तीन)",
    "(चार)",
    "(पाँच)",
    "(छह)",
    "(क)",
    "(ख)",
    "(ग)",
    "संशोधन प्रार्थनापत्र एवं आपत्ति पत्र",
    "अंतर्गत धारा 5 म्याद अधिनियम",
    "वादीगण",
    "प्रतिवादीगण",
    "प्रार्थना",
    "सत्यापन",
]


def _draft() -> dict:
    return {
        "court_heading": "न्यायालय श्रीमान सिविल जज जूनियर डिवीजन बिधूना",
        "case_heading": "मूल वाद संख्या FONT-TEST/2026",
        "parties": ["वादीगण: परीक्षा", "प्रतिवादीगण: परीक्षा"],
        "title": "संशोधन प्रार्थनापत्र एवं आपत्ति पत्र",
        "opening_averment": "अंतर्गत धारा 5 म्याद अधिनियम",
        "pleadings": [
            "यह कि न्यायालय श्रीमान सिविल जज जूनियर डिवीजन बिधूना में यह प्रार्थनापत्र प्रस्तुत है।",
        ],
        "prayer": ["एक दो तीन चार पाँच छह"],
        "verification": "सत्यापन: मैं सत्यापित करता हूँ।",
        "signature_block": ["प्रार्थना", "बिधूना"],
    }


def main() -> int:
    out = ROOT / "tests" / "output" / "font_test"
    out.mkdir(parents=True, exist_ok=True)

    reportlab_pdf = out / "devanagari_reportlab.pdf"
    ldr.render_pdf_reportlab(_draft(), reportlab_pdf, paper="legal")
    print(f"ReportLab PDF: {reportlab_pdf} ({reportlab_pdf.stat().st_size} bytes)")

    docx_path = out / "devanagari_canonical.docx"
    ldr.render_docx(_draft(), docx_path, paper="legal")
    print(f"DOCX: {docx_path}")

    if docx_to_pdf.converter_available():
        canonical_pdf = out / "devanagari_canonical.pdf"
        docx_to_pdf.convert(docx_path, canonical_pdf)
        print(f"LibreOffice PDF: {canonical_pdf}")
        inspect(canonical_pdf)
    else:
        print("LibreOffice not installed; skipped canonical conversion.")

    inspect(reportlab_pdf)
    return 0


def inspect(pdf_path: Path) -> None:
    import fitz

    with fitz.open(str(pdf_path)) as doc:
        text = "\n".join(page.get_text() for page in doc)
        fonts = []
        for page in doc:
            fonts.extend(page.get_fonts())
    print(f"--- {pdf_path.name} ---")
    print("fonts:", sorted({f[3] for f in fonts}))
    missing = [line for line in LINES if line.replace(" ", "") not in text.replace(" ", "").replace("\n", "")]
    if missing:
        print("MISSING from text layer:", missing)
    else:
        print("all probe strings present in text layer")
    boxes = text.count("□") + text.count("\ufffd")
    print(f"box/replacement chars: {boxes}")


if __name__ == "__main__":
    raise SystemExit(main())
