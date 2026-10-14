```python
"""Azure AI Document Intelligence v4 REST integration with Microsoft Foundry (gpt-6.0-luna) & Gemini Vision OCR fallback."""
from __future__ import annotations
import base64, io, logging, os, time
from dataclasses import dataclass
import requests

from .gemini_client import DEFAULT_AZURE_ENDPOINT, DEFAULT_AZURE_MODEL, _normalize_azure_endpoint
from .krutidev_converter import decode_krutidev_hybrid

log = logging.getLogger("legal-bot.ocr")


class DocumentIntelligenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRResult:
    text: str
    page_count: int
    model_id: str


def _endpoint() -> str:
    value = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip().rstrip("/")
    if not value:
        raise DocumentIntelligenceError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not set.")
    return value


def _key() -> str:
    value = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()
    if not value:
        raise DocumentIntelligenceError("AZURE_DOCUMENT_INTELLIGENCE_KEY is not set.")
    return value


def _json_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") or body
        return str(err.get("message") or err)[:1200] if isinstance(err, dict) else str(err)[:1200]
    except Exception:
        return resp.text[:1200]


def _extract_pdf_text_direct(data: bytes) -> OCRResult | None:
    """Extract embedded text from digital PDFs using pypdf before falling back to vision APIs."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages_text = [(page.extract_text() or "").strip() for page in reader.pages]
        joined = "\n\n".join(p for p in pages_text if p).strip()
        if len(joined) >= 40:
            return OCRResult(
                text=decode_krutidev_hybrid(joined),
                page_count=len(reader.pages) or 1,
                model_id="pdf-text-layer",
            )
    except Exception:
        pass
    return None


def _ocr_with_azure_foundry(data: bytes, content_type: str, filename: str) -> OCRResult:
    azure_key = (
        os.getenv("AZURE_AI_KEY", "").strip()
        or os.getenv("FOUNDRY_API_KEY", "").strip()
        or os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    if not azure_key:
        raise DocumentIntelligenceError("AZURE_AI_KEY is not set for Microsoft Foundry OCR fallback.")

    if filename.lower().endswith(".pdf") or content_type == "application/pdf":
        direct = _extract_pdf_text_direct(data)
        if direct is not None:
            return direct

    endpoint = _normalize_azure_endpoint(os.getenv("AZURE_AI_ENDPOINT", "") or DEFAULT_AZURE_ENDPOINT)
    model = os.getenv("AZURE_AI_MODEL", "").strip() or DEFAULT_AZURE_MODEL
    mime = content_type if content_type and content_type != "application/octet-stream" else "image/jpeg"
    if filename.lower().endswith(".png"):
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    prompt = (
        "Extract all readable text and structure from this Indian legal document/image accurately in clean "
        "Unicode Devanagari Hindi and English. Preserve all party names, father/husband names, ages, addresses, "
        "Gata/Khasra numbers, areas, boundaries (चौहद्दी), dates, amounts, court headings, and prayers verbatim. "
        "Return only the extracted text."
    )
    payload: dict = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "api-key": azure_key,
        "Authorization": f"Bearer {azure_key}",
    }
    try:
        r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
    except requests.RequestException as exc:
        raise DocumentIntelligenceError(f"Microsoft Foundry Vision OCR network error: {exc}") from exc
    if not r.ok:
        raise DocumentIntelligenceError(f"Microsoft Foundry Vision OCR HTTP {r.status_code}: {r.text[:1000]}")
    body = r.json()
    try:
        text = body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise DocumentIntelligenceError(f"Microsoft Foundry Vision OCR returned invalid payload: {body}") from exc
    return OCRResult(text=decode_krutidev_hybrid(text), page_count=1, model_id=model)


def _ocr_with_gemini(data: bytes, content_type: str, filename: str) -> OCRResult:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise DocumentIntelligenceError(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / KEY are not set and GEMINI_API_KEY fallback is unavailable."
        )
    model = os.getenv("GEMINI_FALLBACK_MODEL", "") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model.strip()}:generateContent"
    mime = content_type if content_type and content_type != "application/octet-stream" else "image/jpeg"
    if filename.lower().endswith(".pdf"):
        mime = "application/pdf"
    elif filename.lower().endswith(".png"):
        mime = "image/png"
    prompt = (
        "Extract all readable text and structure from this Indian legal document/image accurately in clean "
        "Unicode Devanagari Hindi and English. Preserve all party names, father/husband names, ages, addresses, "
        "Gata/Khasra numbers, areas, boundaries (चौहद्दी), dates, amounts, court headings, and prayers verbatim. "
        "Return only the extracted text."
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    try:
        r = requests.post(url, params={"key": gemini_key}, json=payload, timeout=120)
    except requests.RequestException as exc:
        raise DocumentIntelligenceError(f"Gemini Vision OCR network error: {exc}") from exc
    if not r.ok:
        raise DocumentIntelligenceError(f"Gemini Vision OCR HTTP {r.status_code}: {r.text[:1000]}")
    body = r.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise DocumentIntelligenceError(f"Gemini Vision OCR returned invalid payload: {body}") from exc
    return OCRResult(text=decode_krutidev_hybrid(text), page_count=1, model_id=model.strip())


def analyze_document_bytes(
    data: bytes, content_type: str = "application/octet-stream", filename: str = "document"
) -> OCRResult:
    if not data:
        raise DocumentIntelligenceError("The uploaded document is empty.")
    model = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL", "prebuilt-layout").strip() or "prebuilt-layout"
    if model != "prebuilt-layout":
        raise DocumentIntelligenceError("AZURE_DOCUMENT_INTELLIGENCE_MODEL must be prebuilt-layout.")

    has_azure = bool(
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip()
        and os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY", "").strip()
    )
    if not has_azure:
        if (
            os.getenv("AZURE_AI_KEY", "").strip()
            or os.getenv("FOUNDRY_API_KEY", "").strip()
            or os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        ):
            log.info("Azure Document Intelligence credentials not set; using Microsoft Foundry / PDF fallback.")
            return _ocr_with_azure_foundry(data, content_type, filename)
        if os.getenv("GEMINI_API_KEY", "").strip():
            log.info("Azure Document Intelligence credentials not set; using Gemini Vision OCR fallback.")
            return _ocr_with_gemini(data, content_type, filename)
        _endpoint()
        _key()

    api = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", "2024-11-30").strip() or "2024-11-30"
    timeout = float(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_SECONDS", "600"))
    poll = float(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_POLL_SECONDS", "1.5"))
    url = f"{_endpoint()}/documentintelligence/documentModels/{model}:analyze"
    params = {"api-version": api, "outputContentFormat": "markdown"}
    headers = {"Ocp-Apim-Subscription-Key": _key(), "Content-Type": content_type or "application/octet-stream"}
    payload = {"base64Source": base64.b64encode(data).decode("ascii")}
    try:
        r = requests.post(
            url, params=params, headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=90
        )
    except requests.RequestException as exc:
        raise DocumentIntelligenceError(f"Azure Document Intelligence network error: {exc}") from exc
    if r.status_code not in (200, 202):
        raise DocumentIntelligenceError(f"Azure Document Intelligence rejected {filename}: {_json_error(r)}")
    if r.status_code == 200:
        result = r.json()
    else:
        operation = r.headers.get("Operation-Location") or r.headers.get("operation-location")
        if not operation:
            raise DocumentIntelligenceError("Azure returned 202 without an Operation-Location.")
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise DocumentIntelligenceError("Azure Document Intelligence timed out while analyzing the document.")
            try:
                rr = requests.get(operation, headers={"Ocp-Apim-Subscription-Key": _key()}, timeout=60)
            except requests.RequestException as exc:
                raise DocumentIntelligenceError(f"Azure polling network error: {exc}") from exc
            if rr.status_code != 200:
                raise DocumentIntelligenceError(f"Azure polling failed: {_json_error(rr)}")
            result = rr.json()
            status = str(result.get("status", "")).lower()
            if status == "succeeded":
                break
            if status in {"failed", "cancelled"}:
                raise DocumentIntelligenceError(f"Azure analysis status: {status}. {_json_error(rr)}")
            time.sleep(poll)
    analyze = result.get("analyzeResult") or result.get("result") or result
    text = str(analyze.get("content") or "").strip()
    if not text:
        chunks = []
        for page in analyze.get("pages") or []:
            for line in page.get("lines") or []:
                c = line.get("content")
                if c:
                    chunks.append(str(c))
        text = "\n".join(chunks).strip()
    pages = analyze.get("pages") or []
    return OCRResult(text=decode_krutidev_hybrid(text), page_count=len(pages) or 1, model_id=model)
```
