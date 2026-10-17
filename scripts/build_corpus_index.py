#!/usr/bin/env python3
"""Build Unified Dual-Source Corpus Index (Professional Templates + Advocate B.D. Shukla Corpus).

Usage:
    python3 scripts/build_corpus_index.py [optional_path_to_purchased_templates_folder_or_zip] [optional_output_json]

If no arguments are provided, it automatically:
1. Extracts all zip archives in `legal_corpus/` (dad's 306 files).
2. Scans `templates/` for professional legal templates.
3. Builds a unified index tagging each document as `professional_template` or `dad_corpus`.
4. Saves the resulting index directly to `corpus_index.json` in the project root.
"""
from __future__ import annotations
import os, sys, tempfile, zipfile, shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.drafting_engine.corpus_manager import build_index, save_index, extract_category_zip


def main():
    corpus_dir = ROOT_DIR / "legal_corpus"
    templates_dir = ROOT_DIR / "templates"
    default_out = ROOT_DIR / "corpus_index.json"

    additional_input = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else None
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].strip() else default_out

    with tempfile.TemporaryDirectory(prefix="legal_corpus_build_") as tmp_dir:
        extracted_root = Path(tmp_dir)

        # 1. Extract all existing zip files in legal_corpus/
        print("Extracting advocate corpus archives from legal_corpus/...")
        for zip_file in sorted(corpus_dir.glob("*.zip")):
            cat_name = zip_file.stem
            dest = extracted_root / cat_name
            extract_category_zip(zip_file, dest)
            print(f"  Extracted: {zip_file.name} -> {cat_name}/")

        # 2. Ingest bundled professional templates from templates/
        if templates_dir.exists():
            dest_tpl = extracted_root / "00_Professional_Templates"
            dest_tpl.mkdir(parents=True, exist_ok=True)
            for tpl_file in templates_dir.glob("*.*"):
                if tpl_file.suffix.lower() in {".docx", ".pdf", ".txt", ".rtf"}:
                    shutil.copy2(tpl_file, dest_tpl / tpl_file.name)
            print(f"  Loaded professional templates from templates/: {len(list(dest_tpl.glob('*.*')))} files")

        # 3. Handle additional purchased templates if passed via CLI or in legal_corpus/
        additional_extracted = None
        if additional_input and additional_input.exists():
            print(f"Processing additional templates from: {additional_input}...")
            if additional_input.is_file() and additional_input.suffix.lower() == ".zip":
                additional_extracted = extracted_root / "additional_purchased_templates"
                extract_category_zip(additional_input, additional_extracted)
            elif additional_input.is_dir():
                additional_extracted = additional_input

        print("\nBuilding dual-source index...")
        index = build_index(extracted_root, additional_templates_dir=additional_extracted)
        save_index(index, out_path)

        counts = index.get("source_counts", {})
        print(f"\nSuccessfully generated {out_path}!")
        print(f"Total Indexed Documents: {index.get('document_count', 0)}")
        print(f"  • Professional Templates: {counts.get('professional_template', 0)}")
        print(f"  • Advocate B.D. Shukla Drafts: {counts.get('dad_corpus', 0)}")


if __name__ == "__main__":
    main()
