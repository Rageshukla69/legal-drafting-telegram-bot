#!/usr/bin/env python3
"""Deployment check for the PDF pipeline (runs with production dependencies only).

    heroku run python scripts/check_pdf_pipeline.py
    python scripts/check_pdf_pipeline.py --require-canonical   # exit 1 without LibreOffice

It answers, on the actual host:

* which engine will produce PDFs (LibreOffice conversion of the canonical DOCX,
  or the ReportLab fallback) and why;
* whether ``soffice`` starts and converts the Hindi test DOCX;
* whether the produced PDF embeds the bundled Noto Sans Devanagari faces and
  has real text (a ToUnicode map) rather than images or boxes;
* whether the ReportLab fallback can shape Devanagari (uharfbuzz present).

For the glyph-by-glyph verification use ``scripts/pdf_devanagari_font_test.py``
(needs PyMuPDF from requirements-dev.txt).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.drafting_engine.renderers import docx_to_pdf  # noqa: E402
from app.drafting_engine.renderers import legal_document_renderer as ldr  # noqa: E402
from app.drafting_engine.renderers import reportlab_shaping  # noqa: E402

# Kept in sync with scripts/pdf_devanagari_font_test.py (imported lazily so
# this check does not need PyMuPDF/fontTools at import time).
from pdf_devanagari_font_test import FONT_TEST_DRAFT  # noqa: E402


def _pdf_summary(pdf: Path) -> dict:
    data = pdf.read_bytes()
    fonts = sorted(set(re.findall(rb"/BaseFont\s*/(?:[A-Z]{6}\+)?([A-Za-z0-9_.-]+)", data)))
    pages = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    return {
        "bytes": len(data),
        "is_pdf": data.startswith(b"%PDF-"),
        "pages": pages,
        "fonts": [f.decode("latin-1") for f in fonts],
        "has_tounicode": b"/ToUnicode" in data,
        "has_devanagari_font": any(b"NotoSansDevanagari" in f for f in fonts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--require-canonical", action="store_true", help="exit 1 unless LibreOffice converts the DOCX")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    status = ldr.pdf_pipeline_status()
    report: dict = {"status": status, "checks": {}}
    problems: list[str] = []

    for slot, path in status["fonts"].items():
        if not Path(path).is_file():
            problems.append(f"font file missing for {slot}: {path}")
    if not status["uharfbuzz"]:
        problems.append("uharfbuzz is not installed: the ReportLab fallback cannot shape Devanagari")

    with tempfile.TemporaryDirectory(prefix="legal-pdf-check-") as tmp:
        tmp_path = Path(tmp)
        docx = ldr.render_docx(FONT_TEST_DRAFT, tmp_path / "check.docx", paper="legal")

        if status["soffice"]:
            report["checks"]["soffice_version"] = docx_to_pdf.soffice_version()
            started = time.time()
            try:
                pdf = docx_to_pdf.convert(docx, tmp_path / "check.libreoffice.pdf")
                summary = _pdf_summary(pdf)
                summary["seconds"] = round(time.time() - started, 1)
                report["checks"]["libreoffice"] = summary
                if not summary["is_pdf"] or summary["pages"] < 1:
                    problems.append("LibreOffice produced an unreadable PDF")
                if not summary["has_devanagari_font"]:
                    problems.append("LibreOffice PDF does not embed Noto Sans Devanagari (font substitution)")
                if not summary["has_tounicode"]:
                    problems.append("LibreOffice PDF has no ToUnicode map (text is not extractable)")
            except docx_to_pdf.PdfConversionError as exc:
                report["checks"]["libreoffice"] = {"error": str(exc)}
                problems.append(f"LibreOffice conversion failed: {exc}")
        else:
            report["checks"]["libreoffice"] = {"error": status["reason"]}
            if args.require_canonical or status["require_canonical"]:
                problems.append(status["reason"])

        try:
            pdf = ldr.render_pdf_reportlab(FONT_TEST_DRAFT, tmp_path / "check.reportlab.pdf", paper="legal")
            summary = _pdf_summary(pdf)
            summary["shaper_installed"] = reportlab_shaping.installed()
            report["checks"]["reportlab_fallback"] = summary
            if not summary["has_devanagari_font"] or not summary["shaper_installed"]:
                problems.append("ReportLab fallback is not drawing shaped Devanagari")
        except Exception as exc:  # noqa: BLE001
            report["checks"]["reportlab_fallback"] = {"error": f"{type(exc).__name__}: {exc}"}
            problems.append(f"ReportLab fallback failed: {exc}")

    report["problems"] = problems
    report["verdict"] = "OK" if not problems else "PROBLEMS"
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print("PDF pipeline check")
        print(f"  engine expected : {status['expected_engine']} ({status['reason']})")
        print(f"  soffice         : {status['soffice'] or 'not found'}")
        if report["checks"].get("soffice_version"):
            print(f"  soffice version : {report['checks']['soffice_version']}")
        for name in ("libreoffice", "reportlab_fallback"):
            check = report["checks"].get(name, {})
            if "error" in check:
                print(f"  {name:16}: ERROR {check['error']}")
            elif check:
                print(
                    f"  {name:16}: {check['pages']} page(s), fonts {check['fonts']}, "
                    f"ToUnicode={check['has_tounicode']}"
                    + (f", {check['seconds']}s" if "seconds" in check else "")
                )
        print(f"  verdict         : {report['verdict']}")
        for problem in problems:
            print(f"   - {problem}")
        if status["expected_engine"] != ldr.PDF_ENGINE_LIBREOFFICE and not problems:
            print(
                "  note            : PDFs will come from the ReportLab fallback (Hindi renders "
                "correctly, but pagination is not guaranteed to match the DOCX). Install the "
                "heroku-community/apt buildpack to enable the canonical conversion."
            )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
