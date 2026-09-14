"""Azure AI Document Intelligence integration for image/PDF intake."""
from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO


class DocumentIntelligenceError(RuntimeError):
    """A user-facing error from Azure Document Intelligence."""


@dataclass(frozen=True)
class OCRResult:
    text: str
    page_count: int
    model_id: str


def _client():
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()
    if not endpoint or not key:
        raise DocumentIntelligenceError(
            "Azure Document Intelligence is not configured. Add AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY."
        )
    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.ai.documentintelligence import DocumentIntelligenceClient
    except ImportError as exc:
        raise DocumentIntelligenceError(
            "Azure Document Intelligence SDK is missing. Install azure-ai-documentintelligence."
        ) from exc
    return DocumentIntelligenceClient(
        endpoint=endpoint, credential=AzureKeyCredential(key)
    )


def _fallback_text(result) -> str:
    """Build readable text if the SDK result has no top-level content string."""
    chunks: list[str] = []
    for page in getattr(result, "pages", None) or []:
        lines = getattr(page, "lines", None) or []
        for line in lines:
            content = getattr(line, "content", None)
            if content:
                chunks.append(str(content))
        if lines:
            chunks.append("")
    return "\n".join(chunks).strip()


def analyze_document_bytes(
    data: bytes,
    content_type: str = "application/octet-stream",
    filename: str = "document",
) -> OCRResult:
    """Analyze an image/PDF using Azure's prebuilt-layout model."""
    if not data:
        raise DocumentIntelligenceError("The uploaded document is empty.")

    model_id = os.getenv(
        "AZURE_DOCUMENT_INTELLIGENCE_MODEL", "prebuilt-layout"
    ).strip() or "prebuilt-layout"
    if model_id != "prebuilt-layout":
        raise DocumentIntelligenceError(
            "AZURE_DOCUMENT_INTELLIGENCE_MODEL must be prebuilt-layout for image intake."
        )

    try:
        client = _client()
        poller = client.begin_analyze_document(
            model_id,
            analyze_request=BytesIO(data),
            content_type=content_type or "application/octet-stream",
            output_content_format="markdown",
        )
        result = poller.result()
    except DocumentIntelligenceError:
        raise
    except Exception as exc:
        raise DocumentIntelligenceError(
            f"Azure analysis failed for {filename}: {exc}"
        ) from exc

    text = str(getattr(result, "content", "") or "").strip()
    if not text:
        text = _fallback_text(result)

    pages = getattr(result, "pages", None) or []
    page_count = len(pages) or 1
    return OCRResult(text=text, page_count=page_count, model_id=model_id)
