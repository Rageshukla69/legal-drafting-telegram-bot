# Image/PDF intake

This patch adds Telegram image/PDF intake using **Azure AI Document Intelligence**.

## Heroku Config Vars

Add:

```text
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://legal-drafting-bot-intel.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=<your secret key>
AZURE_DOCUMENT_INTELLIGENCE_MODEL=prebuilt-layout
```

Do not commit the key to Git.

## Pipeline

```text
Telegram photo / image / PDF
        ↓
Azure Document Intelligence
        ↓
prebuilt-layout
        ↓
OCR + document structure
        ↓
existing conservative Gemini intake
        ↓
existing case state + 306-draft retrieval
        ↓
existing deterministic DOCX/PDF renderer
```

`prebuilt-layout` is selected **in code**. No Document Intelligence Studio
configuration is required for the Telegram bot.

The existing Azure Speech voice path is unchanged. Voice notes continue to use
`AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION`.

Users can send multiple photos or supported image/PDF documents sequentially in
the same active case. Each upload is OCR'd and passed through the same intake
pipeline; facts are merged conservatively.

Supported uploads: Telegram photos, JPG/JPEG, PNG, BMP, TIFF, HEIF and PDF.
