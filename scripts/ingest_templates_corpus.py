#!/usr/bin/env python3
"""Dedicated Ingestion Script for Purchased 500+ Hindi Legal Templates.

When you download your 500+ legal templates from your Google Drive link:
https://drive.google.com/drive/folders/1ODXPE-7a1UTVBfImUq2e_n0ShRP5AWrF

Run:
    python3 scripts/ingest_templates_corpus.py /path/to/downloaded_templates_or_zip

This script will:
1. Scan and extract all DOCX, DOC, PDF, and TXT templates.
2. Clean legacy KrutiDev fonts to pure Unicode Devanagari Hindi.
3. Automatically classify each template into Dava, Written Statement, Application,
   Evidence Affidavit, Shapath Patra, Legal Notice, or Other Civil Draft.
4. Merge them with Advocate B.D. Shukla's 306 court drafts.
5. Produce the production-ready `corpus_index.json` with dual-source priority tags!
"""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from scripts.build_corpus_index import main as build_corpus_main


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/ingest_templates_corpus.py <path_to_downloaded_templates_folder_or_zip>")
        print("\nExample:")
        print("  python3 scripts/ingest_templates_corpus.py ~/Downloads/Legal_Hindi_Templates.zip")
        print("  python3 scripts/ingest_templates_corpus.py ~/Downloads/Legal_Hindi_Templates/")
        print("\nRunning default corpus build with bundled professional templates and dad's 306 drafts...")
    build_corpus_main()


if __name__ == "__main__":
    main()
