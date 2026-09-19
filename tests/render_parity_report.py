"""Manual DOCX/PDF parity report for the Dava renderer.

Run:
    python tests/render_parity_report.py [--out tests/output/parity] [--legacy]

For every fixture in ``tests/dava_fixtures.py`` it:
  1. renders the DOCX + PDF through the application renderer,
  2. independently converts the DOCX to PDF with a bare LibreOffice call,
  3. compares page counts, page sizes, extracted text, Unicode integrity and
     rasterised ink, and prints a table.

``--legacy`` additionally renders the deprecated ReportLab PDF path (when it is
still available) so the old mismatch can be shown side by side.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import parity_helpers as ph  # noqa: E402
from dava_fixtures import ALL_FIXTURES  # noqa: E402


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def run(out_root: Path, include_legacy: bool) -> int:
    from app.drafting_engine.renderers import legal_document_renderer as ldr

    reference_available = ph.find_soffice() is not None
    print(f"LibreOffice available: {reference_available} ({ph.find_soffice()})")
    print(f"Fixtures: {len(ALL_FIXTURES)}")
    failures = 0
    rows: list[dict[str, object]] = []

    for name, draft in ALL_FIXTURES.items():
        case_dir = out_root / name
        case_dir.mkdir(parents=True, exist_ok=True)
        docx_path, pdf_path = ldr.render_both(draft, case_dir, base_name=name, paper="legal")

        reference_pdf = None
        if reference_available:
            reference_pdf = ph.reference_docx_to_pdf(docx_path, case_dir / "reference")

        row: dict[str, object] = {"fixture": name}
        row["docx_page_count"] = (
            ph.pdf_page_count(reference_pdf) if reference_pdf else "n/a"
        )
        row["pdf_page_count"] = ph.pdf_page_count(pdf_path)
        row["page_sizes"] = ph.pdf_page_sizes(pdf_path)[:1]
        row["unicode"] = ph.unicode_integrity(pdf_path)

        docx_text = "\n".join(ph.docx_paragraph_texts(docx_path))
        pdf_text = ph.pdf_body_text(pdf_path)
        row["text_match"] = ph.squash(docx_text) == ph.squash(pdf_text)

        if reference_pdf:
            row["page_count_match"] = ph.pdf_page_count(reference_pdf) == ph.pdf_page_count(pdf_path)
            row["page_break_match"] = ph.pdf_first_line_per_page(reference_pdf) == ph.pdf_first_line_per_page(pdf_path)
            row["reference_text_match"] = ph.squash(ph.pdf_body_text(reference_pdf)) == ph.squash(pdf_text)
            compared, ratio = ph.ink_difference(reference_pdf, pdf_path)
            row["ink_diff"] = f"{ratio * 100:.3f}% over {compared} page(s)"

        if include_legacy and hasattr(ldr, "render_pdf_reportlab"):
            legacy_pdf = case_dir / f"{name}_legacy.pdf"
            ldr.render_pdf_reportlab(draft, legacy_pdf, paper="legal")
            row["legacy_pdf_page_count"] = ph.pdf_page_count(legacy_pdf)
            row["legacy_unicode"] = ph.unicode_integrity(legacy_pdf)
            if reference_pdf:
                row["legacy_page_count_match"] = ph.pdf_page_count(reference_pdf) == ph.pdf_page_count(legacy_pdf)
                compared, ratio = ph.ink_difference(reference_pdf, legacy_pdf)
                row["legacy_ink_diff"] = f"{ratio * 100:.3f}% over {compared} page(s)"

        bad = False
        if not row.get("text_match"):
            bad = True
        unicode_stats = row["unicode"]
        if unicode_stats["replacement_chars"] or unicode_stats["private_use_chars"]:
            bad = True
        for key in ("page_count_match", "page_break_match", "reference_text_match"):
            if key in row and not row[key]:
                bad = True
        if bad:
            failures += 1
        rows.append(row)

    print()
    for row in rows:
        print("=" * 78)
        for key, value in row.items():
            if key == "unicode":
                value = (
                    f"replacement={value['replacement_chars']} pua={value['private_use_chars']} "
                    f"control={value['control_chars']} orphan_matra={value['orphan_matra_sequences']} "
                    f"devanagari={value['devanagari_chars']}/{value['total_chars']}"
                )
            print(f"  {key:26} {_fmt(value)}")
        if "legacy_unicode" in row:
            lu = row["legacy_unicode"]
            print(
                f"  {'legacy_unicode':26} replacement={lu['replacement_chars']} pua={lu['private_use_chars']} "
                f"devanagari={lu['devanagari_chars']}/{lu['total_chars']}"
            )

    print("=" * 78)
    print(f"Artifacts written under: {out_root}")
    print(f"FAILURES: {failures}/{len(rows)}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "tests" / "output" / "parity"))
    parser.add_argument("--legacy", action="store_true", help="also render the legacy ReportLab PDF")
    args = parser.parse_args()
    return run(Path(args.out), args.legacy)


if __name__ == "__main__":
    raise SystemExit(main())
