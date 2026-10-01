from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.drafting_engine.renderers import docx_to_pdf
from app.drafting_engine.renderers.legal_document_renderer import canonical_pdf_available, pdf_engine


def _bool_env(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    soffice = docx_to_pdf.find_soffice()
    engine = pdf_engine()
    require_canonical = _bool_env("LEGAL_PDF_REQUIRE_CANONICAL")
    converter = docx_to_pdf.converter_available()
    canonical = canonical_pdf_available()
    report = {
        "pdf_engine": engine,
        "soffice_path": soffice,
        "converter_available": converter,
        "canonical_pdf_available": canonical,
        "require_canonical": require_canonical,
        "status": "ok" if canonical or (not require_canonical) else "error",
        "message": (
            "Canonical DOCX->PDF conversion is available."
            if canonical
            else "Canonical DOCX->PDF conversion is unavailable."
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if require_canonical and not canonical:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
