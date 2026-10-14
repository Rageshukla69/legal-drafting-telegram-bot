from __future__ import annotations
import json, logging, os, re
from typing import Any
from urllib.parse import urlparse

import requests

from .http_retry import RETRYABLE_STATUS as _RETRYABLE_STATUS
from .http_retry import build_retrying_session

log = logging.getLogger("legal-bot.ai")

DEFAULT_AZURE_ENDPOINT = "https://vakilbot.services.ai.azure.com/openai/v1/chat/completions"
DEFAULT_AZURE_MODEL = "gpt-6.0-luna"


class GeminiError(RuntimeError):
    """Raised for any failed AI model call (Microsoft Foundry GPT-6.0-Luna or Gemini)."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _schema(s: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(s, dict):
        return s
    out = {}
    m = {
        "object": "OBJECT",
        "array": "ARRAY",
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
    }
    if s.get("type"):
        out["type"] = m.get(str(s["type"]).lower(), str(s["type"]).upper())
    if "properties" in s:
        out["properties"] = {k: _schema(v) for k, v in s["properties"].items()}
    if "items" in s:
        out["items"] = _schema(s["items"])
    for k in ("required", "enum", "description", "minItems", "maxItems", "minimum", "maximum"):
        if k in s:
            out[k] = s[k]
    return out


def _normalize_azure_endpoint(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        return DEFAULT_AZURE_ENDPOINT
    # Preserve full URLs that already contain /chat/completions or /responses (including ?api-version=...)
    if "/chat/completions" in url or "/responses" in url:
        return url
    url = url.rstrip("/")
    if url.endswith("/openai/v1"):
        return f"{url}/chat/completions"
    return f"{url}/openai/v1/chat/completions"


def _base_host_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://vakilbot.services.ai.azure.com"


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Response did not contain a valid JSON object.")


def _apply_schema_defaults(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(schema, dict):
        return data
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in data or data[key] is None:
            prop_schema = props.get(key) or {}
            ptype = str(prop_schema.get("type", "")).lower()
            if ptype == "array":
                data[key] = []
            elif ptype == "object":
                data[key] = {}
            elif ptype in {"integer", "number"}:
                data[key] = 0
            elif ptype == "boolean":
                data[key] = False
            else:
                data[key] = ""
    return data


class GeminiClient:
    """Unified LLM client supporting Microsoft Foundry (`gpt-6.0-luna`) as primary default and Google Gemini as fallback."""

    def __init__(self):
        self.azure_key = (
            os.getenv("AZURE_AI_KEY", "").strip()
            or os.getenv("FOUNDRY_API_KEY", "").strip()
            or os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        self.azure_endpoint = _normalize_azure_endpoint(
            os.getenv("AZURE_AI_ENDPOINT", "")
            or os.getenv("FOUNDRY_ENDPOINT", "")
            or os.getenv("AZURE_OPENAI_ENDPOINT", "")
            or DEFAULT_AZURE_ENDPOINT
        )
        self.azure_model = (
            os.getenv("AZURE_AI_MODEL", "").strip()
            or os.getenv("FOUNDRY_MODEL", "").strip()
            or os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
            or DEFAULT_AZURE_MODEL
        )

        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
        self.fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()

        provider_pref = os.getenv("AI_PROVIDER", "").strip().lower()
        if not self.azure_key and self.key:
            low_model = self.model.lower()
            if (
                provider_pref in {"azure", "foundry", "openai", "gpt"}
                or low_model.startswith(("gpt", "luna", "o1", "o3", "o4", "grok"))
                or (not self.key.startswith("AIza") and len(self.key) >= 24)
            ):
                self.azure_key = self.key
                if low_model.startswith(("gpt", "luna", "o1", "o3", "o4", "grok")):
                    self.azure_model = self.model
                self.key = ""

        self.timeout = float(os.getenv("AZURE_AI_TIMEOUT_SECONDS", os.getenv("GEMINI_TIMEOUT_SECONDS", "120")))
        self.max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
        self.retry_backoff_seconds = float(os.getenv("GEMINI_RETRY_BACKOFF_SECONDS", "1.5"))
        self.retry_backoff_max_seconds = float(os.getenv("GEMINI_RETRY_BACKOFF_MAX_SECONDS", "60"))

        if not self.azure_key and not self.key:
            raise GeminiError(
                "Neither AZURE_AI_KEY (Microsoft Foundry gpt-6.0-luna) nor GEMINI_API_KEY is set."
            )

        self._session = build_retrying_session(
            max_retries=self.max_retries,
            backoff_factor=self.retry_backoff_seconds,
            backoff_max=self.retry_backoff_max_seconds,
        )

    def _post_azure_chat(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> requests.Response:
        r = self._session.post(url, headers=headers, json=payload, timeout=self.timeout)
        # Newer GPT-5 / GPT-6 models on Azure Foundry may reject temperature=0; retry without temperature if 400
        if r.status_code == 400 and "temperature" in r.text.lower() and "temperature" in payload:
            fallback_payload = {k: v for k, v in payload.items() if k != "temperature"}
            r = self._session.post(url, headers=headers, json=fallback_payload, timeout=self.timeout)
        return r

    def _call_azure(self, *, model: str, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        schema_block = (
            "\n\nCRITICAL JSON OUTPUT REQUIREMENT:\n"
            "Return ONLY a single valid JSON object (no markdown fences, no commentary) "
            "strictly conforming to this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": f"{system}{schema_block}"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Content-Type": "application/json",
            "api-key": self.azure_key,
            "Authorization": f"Bearer {self.azure_key}",
        }
        try:
            r = self._post_azure_chat(self.azure_endpoint, headers, payload)
            # If /openai/v1/chat/completions returns 404 for an Azure OpenAI deployment or slug variant,
            # automatically try the Azure OpenAI deployments path and common GPT-6.0-Luna slug variants.
            if r.status_code == 404:
                base_host = _base_host_url(self.azure_endpoint)
                candidate_models = [model]
                for m_var in ("gpt-6.0-luna", "gpt-6-luna", "gpt-6.0", "gpt-6-0-luna"):
                    if m_var not in candidate_models:
                        candidate_models.append(m_var)
                for cand_m in candidate_models:
                    cand_payload = {**payload, "model": cand_m}
                    for cand_url in (
                        self.azure_endpoint,
                        f"{base_host}/openai/deployments/{cand_m}/chat/completions?api-version=2025-01-01-preview",
                    ):
                        if cand_url == self.azure_endpoint and cand_m == model:
                            continue
                        r_try = self._post_azure_chat(cand_url, headers, cand_payload)
                        if r_try.ok:
                            r = r_try
                            break
                    if r.ok:
                        break
        except requests.RequestException as e:
            raise GeminiError(f"Azure AI Foundry network error ({model}): {e}", retryable=True) from e

        if not r.ok:
            retryable = r.status_code in _RETRYABLE_STATUS
            raise GeminiError(
                f"Azure AI Foundry HTTP {r.status_code} ({model}): {r.text[:2000]}",
                status_code=r.status_code,
                retryable=retryable,
            )
        try:
            body = r.json()
            if "choices" in body:
                text = body["choices"][0]["message"]["content"]
            elif "output_text" in body:
                text = body["output_text"]
            else:
                text = body["output"][0]["content"][0]["text"]
            result = _extract_json_object(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise GeminiError(
                f"Azure AI Foundry returned invalid structured JSON ({model}): {r.text[:2000]}"
            ) from e
        return _apply_schema_defaults(result, schema)

    def _call_gemini(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], thinking_level: str
    ) -> dict[str, Any]:
        url = f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){model}:generateContent"
        gen: dict[str, Any] = {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _schema(schema),
        }
        if model.startswith("gemini-3") or "thinking" in model:
            gen["thinkingConfig"] = {"thinkingLevel": thinking_level}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }
        try:
            r = self._session.post(url, params={"key": self.key}, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise GeminiError(f"Gemini network error ({model}): {e}", retryable=True) from e

        if not r.ok:
            retryable = r.status_code in _RETRYABLE_STATUS
            raise GeminiError(
                f"Gemini HTTP {r.status_code} ({model}) after up to {self.max_retries + 1} attempt(s): {r.text[:2000]}",
                status_code=r.status_code,
                retryable=retryable,
            )
        try:
            body = r.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            result = _extract_json_object(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise GeminiError(f"Gemini returned invalid structured JSON ({model}): {r.text[:2000]}") from e
        return _apply_schema_defaults(result, schema)

    def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any], thinking_level: str = "medium"
    ) -> dict[str, Any]:
        level = str(thinking_level or "medium").lower()
        if level not in {"low", "medium", "high"}:
            level = "medium"

        last_err: GeminiError | None = None

        if self.azure_key:
            try:
                return self._call_azure(
                    model=self.azure_model,
                    system=system,
                    prompt=prompt,
                    schema=schema,
                )
            except GeminiError as e:
                last_err = e
                log.warning("Azure AI Foundry model '%s' failed: %s", self.azure_model, e)
                if not self.key:
                    raise

        if self.key:
            models_to_try = [self.model]
            if self.fallback_model and self.fallback_model != self.model:
                models_to_try.append(self.fallback_model)

            for i, model in enumerate(models_to_try):
                try:
                    if i > 0 or last_err is not None:
                        log.warning("Falling back to Gemini model '%s'.", model)
                    return self._call_gemini(
                        model=model,
                        system=system,
                        prompt=prompt,
                        schema=schema,
                        thinking_level=level,
                    )
                except GeminiError as e:
                    last_err = e
                    log.warning("Gemini call failed on model '%s': %s", model, e)
                    continue

        assert last_err is not None
        raise last_err
                    
