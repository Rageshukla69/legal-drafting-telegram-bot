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
DEFAULT_OPENAI_API_VERSION = "2024-08-01-preview"


class GeminiError(RuntimeError):
    """Raised for any failed AI model call (Microsoft Foundry GPT-6.0-Luna, Azure OpenAI, or Gemini)."""

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
    """Unified LLM client supporting Microsoft Foundry (`gpt-6.0-luna`) as primary default,
    Azure OpenAI (`gpt-4.1-mini`) as secondary fallback, and Google Gemini as tertiary fallback.
    """

    def __init__(self):
        # Primary Azure AI / Microsoft Foundry settings
        self.azure_key = (
            os.getenv("AZURE_AI_KEY", "").strip()
            or os.getenv("FOUNDRY_API_KEY", "").strip()
        )
        self.azure_endpoint = _normalize_azure_endpoint(
            os.getenv("AZURE_AI_ENDPOINT", "")
            or os.getenv("FOUNDRY_ENDPOINT", "")
            or DEFAULT_AZURE_ENDPOINT
        )
        self.azure_model = (
            os.getenv("AZURE_AI_MODEL", "").strip()
            or os.getenv("FOUNDRY_MODEL", "").strip()
            or DEFAULT_AZURE_MODEL
        )

        # Secondary Azure OpenAI deployment fallback settings
        self.azure_openai_key = (
            os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            or self.azure_key
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        self.azure_openai_endpoint = (
            os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
            or os.getenv("AZURE_AI_ENDPOINT", "").strip()
            or self.azure_endpoint
        )
        self.azure_openai_deployment = (
            os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
            or "gpt-4.1-mini"
        )

        # If primary azure_key was not set, fall back to azure_openai_key
        if not self.azure_key and self.azure_openai_key:
            self.azure_key = self.azure_openai_key

        # Google Gemini settings
        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
        self.fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()

        # Handle provider configuration edge-cases
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

        self._session = build_retrying_session(
            max_retries=self.max_retries,
            backoff_factor=self.retry_backoff_seconds,
            backoff_max=self.retry_backoff_max_seconds,
        )

    def _post_azure_chat(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> requests.Response:
        r = self._session.post(url, headers=headers, json=payload, timeout=self.timeout)
        # Newer GPT models on Azure Foundry may reject temperature=0; retry without temperature if 400
        if r.status_code == 400 and "temperature" in r.text.lower() and "temperature" in payload:
            fallback_payload = {k: v for k, v in payload.items() if k != "temperature"}
            r = self._session.post(url, headers=headers, json=fallback_payload, timeout=self.timeout)
        return r

    def _call_azure(
        self,
        *,
        model: str,
        endpoint: str,
        api_key: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
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
            "api-key": api_key,
            "Authorization": f"Bearer {api_key}",
        }

        # Build candidate URLs
        base_host = _base_host_url(endpoint)
        candidate_urls = [endpoint]
        if "/openai/deployments/" not in endpoint:
            # Also try the Azure OpenAI deployments endpoint format
            dep_url = f"{base_host}/openai/deployments/{model}/chat/completions?api-version={DEFAULT_OPENAI_API_VERSION}"
            if dep_url not in candidate_urls:
                candidate_urls.append(dep_url)

        last_response = None
        for cand_url in candidate_urls:
            try:
                r = self._post_azure_chat(cand_url, headers, payload)
                last_response = r
                if r.ok:
                    break
                # If 404, try model name variants if model is gpt-6.0-luna
                if r.status_code == 404 and "luna" in model.lower():
                    for m_var in ("gpt-6.0-luna", "gpt-6-luna", "gpt-6.0", "gpt-6-0-luna"):
                        cand_payload = {**payload, "model": m_var}
                        r_try = self._post_azure_chat(cand_url, headers, cand_payload)
                        if r_try.ok:
                            r = r_try
                            last_response = r
                            break
                    if r.ok:
                        break
            except requests.RequestException as e:
                raise GeminiError(f"Azure AI network error ({model} at {cand_url}): {e}", retryable=True) from e

        if last_response is None or not last_response.ok:
            status_code = last_response.status_code if last_response is not None else 500
            err_text = last_response.text[:2000] if last_response is not None else "No response"
            retryable = status_code in _RETRYABLE_STATUS
            raise GeminiError(
                f"Azure AI HTTP {status_code} ({model}): {err_text}",
                status_code=status_code,
                retryable=retryable,
            )

        try:
            body = last_response.json()
            if "choices" in body:
                text = body["choices"][0]["message"]["content"]
            elif "output_text" in body:
                text = body["output_text"]
            else:
                text = body["output"][0]["content"][0]["text"]
            result = _extract_json_object(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise GeminiError(
                f"Azure AI returned invalid structured JSON ({model}): {last_response.text[:2000]}"
            ) from e

        return _apply_schema_defaults(result, schema)

    def _call_gemini(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any], thinking_level: str
    ) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
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
        """Generate structured JSON with automatic multi-tier fallback:
        Tier 1: Primary Microsoft Foundry GPT-6.0-Luna
        Tier 2: Secondary Azure OpenAI Deployment (gpt-4.1-mini)
        Tier 3: Google Gemini (if configured)
        """
        if not self.azure_key and not self.azure_openai_key and not self.key:
            raise GeminiError(
                "No AI API keys configured. Set AZURE_AI_KEY (for gpt-6.0-luna), "
                "AZURE_OPENAI_API_KEY (for gpt-4.1-mini), or GEMINI_API_KEY."
            )

        level = str(thinking_level or "medium").lower()
        if level not in {"low", "medium", "high"}:
            level = "medium"

        errors: list[str] = []

        # Tier 1: Try Primary Azure AI / Microsoft Foundry (gpt-6.0-luna)
        if self.azure_key:
            try:
                return self._call_azure(
                    model=self.azure_model,
                    endpoint=self.azure_endpoint,
                    api_key=self.azure_key,
                    system=system,
                    prompt=prompt,
                    schema=schema,
                )
            except GeminiError as e:
                log.warning("Primary Azure AI model '%s' failed: %s. Attempting fallback.", self.azure_model, e)
                errors.append(f"Primary ({self.azure_model}): {e}")

        # Tier 2: Try Secondary Azure OpenAI Deployment (gpt-4.1-mini) if different or if primary failed
        if self.azure_openai_key and (self.azure_openai_deployment != self.azure_model or not self.azure_key):
            try:
                log.info("Invoking secondary fallback Azure deployment: %s", self.azure_openai_deployment)
                return self._call_azure(
                    model=self.azure_openai_deployment,
                    endpoint=_normalize_azure_endpoint(self.azure_openai_endpoint),
                    api_key=self.azure_openai_key,
                    system=system,
                    prompt=prompt,
                    schema=schema,
                )
            except GeminiError as e:
                log.warning("Secondary Azure deployment '%s' failed: %s.", self.azure_openai_deployment, e)
                errors.append(f"Secondary ({self.azure_openai_deployment}): {e}")

        # Tier 3: Try Gemini models if configured
        if self.key:
            models_to_try = [self.model]
            if self.fallback_model and self.fallback_model != self.model:
                models_to_try.append(self.fallback_model)

            for i, model in enumerate(models_to_try):
                try:
                    log.info("Invoking tertiary fallback Gemini model: %s", model)
                    return self._call_gemini(
                        model=model,
                        system=system,
                        prompt=prompt,
                        schema=schema,
                        thinking_level=level,
                    )
                except GeminiError as e:
                    log.warning("Gemini model '%s' failed: %s", model, e)
                    errors.append(f"Gemini ({model}): {e}")
                    continue

        raise GeminiError(
            "All configured AI model endpoints failed:\n" + "\n".join(f"- {err}" for err in errors)
        )
