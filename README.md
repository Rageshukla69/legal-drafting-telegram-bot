# Legal Drafting Telegram Bot

A Telegram-based legal drafting assistant built for the first Dava/Plaint
workflow. This repository is the Phase 5 foundation and connects the Telegram
interface to the Dava conversation/RAG/Azure drafting engine.

## Current milestone

Implemented:
- Telegram `/start`, `/newcase`, `/cancel`
- persistent SQLite case sessions
- text message intake
- conservative labeled-fact parser
- missing-information questions
- Dava RAG/composer integration
- Azure Structured Outputs adapter
- safe environment-variable configuration
- Heroku `Procfile`

Not yet implemented:
- voice transcription
- photo/PDF OCR
- automatic natural-language fact extraction
- final DOCX generation
- all seven document categories
- production authentication/authorization

## Important

Do NOT put Telegram or Azure API keys in GitHub. Use environment variables.
The `.gitignore` already excludes `.env` and the SQLite database.

## Local test

Python 3.12 is recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set:
- `TELEGRAM_BOT_TOKEN`
- Azure variables when live drafting is enabled

Then:

```bash
python -m app.bot
```

## Test input

The first milestone parser recognizes explicit labels such as:

```text
न्यायालय: न्यायालय श्रीमान सिविल जज जूनियर डिवीजन, बिधूना
वादी: राम कुमार
प्रतिवादी: श्याम कुमार
तथ्य: प्रतिवादी ने विवादित संपत्ति पर कब्जा किया।
राहत: स्थायी निषेधाज्ञा
```

This is deliberately conservative. The natural-language Azure fact-extraction adapter will replace this starter parser in the next milestone.

## Deployment

The repository is structured for a Heroku worker:

`worker: python -m app.bot`

Set the required environment variables in Heroku Config Vars. Never commit secrets.


## Azure v1 configuration

The Azure adapter uses the OpenAI Python client with the Azure OpenAI v1
endpoint. Configure these Heroku Config Vars:

- `TELEGRAM_BOT_TOKEN`
- `AZURE_OPENAI_ENDPOINT` (for example, `https://<resource>.services.ai.azure.com/openai/v1`)
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT` (for example, `gpt-4.1-mini`)

`AZURE_OPENAI_API_VERSION` is not required by this v1 adapter.

## Package/import note

Run the bot from the repository root with:

```bash
python -m app.bot
```

All internal imports use the `app.*` package path or relative imports, so the
Heroku worker does not depend on the current working directory being `app/`.


## Document outputs

The Dava pipeline now renders the same structured draft into both:

- `.docx` editable legal document
- `.pdf` print/share document

Both are generated deterministically from the same JSON draft. The LLM does not
generate the DOCX/PDF directly.
