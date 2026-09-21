# Heroku + Azure Cosmos DB

Heroku only needs the Azure Cosmos endpoint/key. The Cosmos account remains in Azure; do not create a second database on Heroku.

## Config vars

```bash
heroku login
heroku git:remote -a YOUR_HEROKU_APP
heroku config:set TELEGRAM_BOT_TOKEN="..."
heroku config:set GEMINI_API_KEY="..."
heroku config:set GEMINI_MODEL="gemini-3.8-flash"
heroku config:set GEMINI_FALLBACK_MODEL="gemini-2.5-flash"
heroku config:set AZURE_SPEECH_KEY="..."
heroku config:set AZURE_SPEECH_REGION="..."
heroku config:set COSMOS_ENDPOINT="https://YOUR-ACCOUNT.documents.azure.com:443/"
heroku config:set COSMOS_KEY="YOUR_PRIMARY_KEY"
heroku config:set COSMOS_DATABASE="legal_drafting_bot"
heroku config:set COSMOS_CONTAINER="cases"
heroku config:set COSMOS_THROUGHPUT="400"
heroku config:set LEGAL_PAPER="legal"
```

Verify with `heroku config`. Heroku Config Vars are exposed to the dyno as environment variables; keep secrets out of Git.

## Deploy

PDF files are converted from the canonical DOCX with headless LibreOffice.
Add the apt buildpack **before** the Python buildpack so `soffice` is on the dyno:

```bash
heroku buildpacks:add --index 1 heroku-community/apt
```

The committed `Aptfile` installs `libreoffice-writer` and `libreoffice-core`.
Bundled Devanagari fonts in `app/drafting_engine/assets/fonts/` are staged into
a private fontconfig dir for each conversion. If `soffice` is not on `PATH`
after deploy:

```bash
heroku config:set SOFFICE_BIN=/app/.apt/usr/bin/soffice
heroku config:set LEGAL_PDF_REQUIRE_CANONICAL=1
```

`LEGAL_PDF_REQUIRE_CANONICAL=1` fails instead of falling back to ReportLab
(which paginates independently of the DOCX).

```bash
git add .
git commit -m "upgrade legal drafting bot to seven document workflows"
git push heroku main
```

The Procfile remains `worker: python -m app.bot`.
