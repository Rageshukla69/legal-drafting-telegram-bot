# DOCX ↔ PDF parity

The generated DOCX and PDF are supposed to be the same legal document. They used
to be produced by two independent layout engines, so they disagreed on line
wrapping, paragraph heights and page breaks, and the PDF's Hindi text was
damaged. This document records the root cause, the fix, and how to verify it.

## Root cause

The renderer built both outputs from the Dava JSON, but with **two independent
layout engines**:

* DOCX — `python-docx`, laid out later by Word / LibreOffice / a phone viewer.
* PDF — ReportLab `BaseDocTemplate`, laying the same JSON out itself.

Nothing kept those two layouts in step, so:

1. **Pagination, wrapping and spacing diverged.** Different font metrics, a
   different line-breaking algorithm (Devanagari clusters are handled
   differently), and different paragraph-height calculations produced different
   line counts, and therefore different page breaks. Measured on the test
   fixtures: the pages of the ReportLab PDF differed from the DOCX rendering on
   every fixture, with 6–9 % of page pixels differing.
2. **The PDF text layer was corrupted.** ReportLab shapes Devanagari with
   HarfBuzz and then has to map the resulting *glyphs* back to Unicode. Conjuncts
   and half-forms (न्, श्री, स्व, त्र, क्ष …) and reordered matras have no single
   codepoint, so they were written into the ToUnicode CMap as Private Use Area
   codepoints or dropped entirely. Extracting a real draft produced
   `यायालय ीमान सवल जज` for `न्यायालय श्रीमान सिविल जज जज`, with 100–335
   private-use characters per document. PDF search and copy-paste were useless,
   and legal section headings such as **प्रार्थना** and **सत्यापन** could not be
   found in the produced PDF at all.
3. **Page numbering only existed in the PDF.** The DOCX had no footer.

A second, unrelated defect was found in the same module while testing: the party
splitter tested the plaintiff label `वादी` before the defendant label
`प्रतिवादी`. Because "प्रतिवादी" contains the substring "वादी", labelled
defendants were classified as plaintiffs, so the legally required **बनाम**
separator was dropped whenever the model labelled the parties (the normal case).

## Architecture before

```
Dava JSON
   ├──→ python-docx ──────→ DOCX          (layout engine A)
   └──→ ReportLab ────────→ PDF           (layout engine B)

two layouts, never compared, free to disagree
```

## Architecture after

```
Dava JSON
   ↓
python-docx                       ← the single layout source
   ↓
canonical DOCX
   ↓
LibreOffice headless (docx_to_pdf.convert)
   ↓
PDF                               ← faithful conversion of that DOCX
```

`render_both()` renders the DOCX once and converts **that exact file**, so the
PDF cannot be stale or independently paginated. `render_pdf()` keeps its
original signature and builds a temporary canonical DOCX when it is called on
its own.

## Files

| File | Change |
| --- | --- |
| `app/drafting_engine/renderers/docx_to_pdf.py` | **new** — headless LibreOffice converter (discovery, isolated profile/HOME, bundled fonts, timeouts, clear errors) |
| `app/drafting_engine/renderers/legal_document_renderer.py` | PDF now converted from the canonical DOCX; `render_pdf_reportlab` kept as fallback; page-number footer added to the DOCX; font coverage/name caching; `बनाम` party-split fix |
| `app/drafting_engine/assets/fonts/DejaVuSans.ttf` | **new** — Latin + symbol fallback the code already expected to find there |
| `Aptfile` | **new** — Heroku system packages (LibreOffice) |
| `requirements-dev.txt` | **new** — pytest + pymupdf for the parity checks |
| `tests/dava_fixtures.py` | **new** — five synthetic test Dava drafts |
| `tests/parity_helpers.py` | **new** — independent DOCX→PDF reference, text/Unicode/pixel comparison helpers |
| `tests/test_docx_pdf_parity.py` | **new** — parity, Unicode and legal-content regression tests |
| `tests/render_parity_report.py` | **new** — runnable report that writes artifacts and prints metrics |
| `tests/test_pdf_shaping_markup.py` | one assertion corrected (see "Pre-existing failures") |
| `.gitignore` | ignore generated `tests/output/` |

Nothing else was touched: the Telegram bot, case intake, corpus retrieval and
index, the Dava schema, validator, composer, structure planner, orchestrator,
case state, drafting prompts and the 306-draft corpus are unchanged.

## Preserved legal format

The renderer still emits, in this order and with the existing rules: court
heading (centred, bold), case heading, plaintiff block, **बनाम**, defendant
block, document title (centred, bold, underlined), opening averment, numbered
paragraphs (`1 (एक) : …`), **प्रार्थना** with its fixed lead-in and Devanagari
lettered clauses, signature/advocate block (right aligned), and **सत्यापन**.
Layout page-break instructions (`layout.page_break_before`) are still honoured
by both outputs because both come from the same DOCX.

Legal text is never rewritten: paragraph numbering stays renderer-assigned, and
names, dates, case numbers, reliefs and verification wording pass through
untouched (asserted by `test_legal_content_is_rendered_verbatim` and
`test_numbering_and_dates_are_preserved`).

## Linux / Heroku

`soffice` must exist on the dyno. Install it at build time with Heroku's apt
buildpack (Cedar generation):

```bash
heroku buildpacks:add --index 1 heroku-community/apt
git push heroku main
```

The committed `Aptfile` installs `libreoffice-writer` and `libreoffice-core`.
Notes:

* **Fonts need no packages.** `NotoSansDevanagari-Regular/Bold.ttf` and
  `DejaVuSans.ttf` ship in `app/drafting_engine/assets/fonts/` and are copied by
  the converter into a private `HOME` (`.fonts` and `.local/share/fonts`) so
  `fontconfig` finds them even on a host with no fonts installed. This is why
  the DOCX now names `Noto Sans Devanagari` / `DejaVu Sans` — the faces that are
  actually present.
* **Font resolution is pinned, not inherited.** The converter also points
  `FONTCONFIG_FILE` at a config that exposes *only* those bundled files. Font
  choice changes glyph metrics (and therefore line breaking) and the PDF's
  ToUnicode CMap, so resolving against whatever the host has installed made the
  same draft render differently on two machines — a newer Noto Sans Devanagari
  build extracted `श््रीमान` where the bundled build extracts `श्रीमान`. Every
  family the DOCX names is a family this repository ships, so pinning is both
  sufficient and deterministic.
* The bundled font is byte-identical to the distribution build
  (`v2.001`, sha256 `79a47036…`), so pinning changes nothing on a normal Linux
  image; it only removes the dependency.
* **No GUI, no shared state.** Every conversion gets its own temporary `HOME`,
  font directory and `-env:UserInstallation` profile, so concurrent users cannot
  collide on a LibreOffice profile lock, and nothing is left behind.
* **Slug size.** LibreOffice adds roughly 240 MB under `/usr/lib/libreoffice`.
  Check `heroku apps:info` after the first deploy if you are near a slug limit;
  on Ubuntu 24.04 stacks `libreoffice-core-nogui` + `libreoffice-writer-nogui`
  are smaller alternatives. If the slug limit is a hard blocker, the only change
  needed is a different converter inside `docx_to_pdf.convert()` (a container or
  a hosted conversion API) — the rest of the pipeline is unaffected.
* **Memory/time.** One conversion takes roughly 1–3 s and a few hundred MB of
  RAM. The bot runs as a `worker` dyno (`Procfile`), which is not subject to the
  web-dyno 30 s timeout.
* If `soffice` is not on `PATH` after the build, set `SOFFICE_BIN` to its full
  path (for example `/app/.apt/usr/bin/soffice`).

### Optional configuration variables

None are required — the defaults are already correct.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LEGAL_PDF_ENGINE` | `canonical` | `reportlab` forces the legacy independent PDF layout (not recommended) |
| `LEGAL_PDF_REQUIRE_CANONICAL` | `0` | `1` raises instead of falling back when LibreOffice is missing |
| `SOFFICE_BIN` | auto-discovered | explicit LibreOffice binary path |
| `DOCX_PDF_TIMEOUT_SECONDS` | `300` | per-conversion timeout |

Without LibreOffice the renderer logs a warning and falls back to the legacy
ReportLab PDF so the bot still delivers documents, but that layout can paginate
differently from the DOCX and its Hindi text layer is not reliable. Install
LibreOffice (or set `LEGAL_PDF_REQUIRE_CANONICAL=1` to make the problem
visible).

## Verification

```bash
pip install -r requirements-dev.txt
python -m pytest tests/test_docx_pdf_parity.py -q   # parity + Unicode regression
python tests/render_parity_report.py --legacy       # human-readable report
```

`tests/render_parity_report.py` renders every fixture, independently converts
the DOCX with a bare `soffice` call (so a converter bug — wrong paper size, a
stale staged file — cannot make the check pass), and reports page counts, page
sizes, per-page first lines, extracted text, Unicode integrity and a rasterised
ink difference. Both sides pin the same bundled fonts, so the comparison
measures layout parity rather than font availability. `--legacy` also renders
the old ReportLab PDF for comparison. Artifacts go to `tests/output/parity/`.

## Pre-existing failures unrelated to rendering

* `tests/phase7_2_smoke_test.py` — a smoke *script* that pytest also collects
  (`*_test.py`). Its module-level assertion expects `normalize_structure()` to
  substitute the default opening, but the script itself passes
  `"opening": "कुछ भी"`, so it asserts against its own input. Planner logic was
  not touched.
* `tests/test_multi_document_case.py` — imports `app.bot`, which requires
  `OWNER_TELEGRAM_ID` (and other real configuration) to be set.
* `tests/test_pdf_shaping_markup.py::test_latin_identifiers_use_latin_font_without_breaking_hindi`
  asserted that the Latin font run starts exactly at `1092`. The untouched
  `_pdf_inline_font_markup()` keeps ASCII punctuation in the Latin run, so the
  run starts at the preceding `.`. This failing the same way with every
  candidate Latin font (system Noto Sans, system DejaVu, bundled DejaVu) showed
  it was not caused by the font change; the assertion was corrected to test the
  documented behaviour (Devanagari not split, Latin routed to the Latin font).

## Limitations

* LibreOffice (and Word) is a third layout engine: the DOCX opened in Microsoft
  Word may still paginate slightly differently from the PDF. Converting the
  DOCX with the same engine the PDF comes from is the closest guarantee
  available on a Linux server.
* The PDF text layer is real selectable Unicode Devanagari, verified by
  extraction, but extraction of complex scripts is heuristic — a line may be
  reported as two spans when a matra is reordered. Rasterised ink comparison
  confirmed this is an extraction artifact, not a visual gap.
* If the repo is deployed without LibreOffice and without
  `LEGAL_PDF_REQUIRE_CANONICAL=1`, the bot silently uses the legacy layout.
