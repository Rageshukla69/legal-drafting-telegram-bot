from __future__ import annotations
import json, logging, os, re
from typing import Any
from urllib.parse import urlparse

import requests

from .http_retry import RETRYABLE_STATUS as _RETRYABLE_STATUS
from .http_retry import build_retrying_session

log = logging.getLogger("legal-bot.luna")

DEFAULT_AZURE_ENDPOINT = "https://vakilbot.services.ai.azure.com/openai/v1/chat/completions"
DEFAULT_AZURE_MODEL = "gpt-6.0-luna"
DEFAULT_OPENAI_API_VERSION = "2024-08-01-preview"


class LunaError(RuntimeError):
    """Raised for any failed AI model call on Microsoft Foundry GPT-6.0-Luna / Azure OpenAI."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


# Backwards-compatible alias for existing module imports
GeminiError = LunaError


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


class LunaClient:
    """Primary Microsoft Foundry `gpt-6.0-luna` Legal Intelligence Client.

    Routes all legal drafting, intake, and precision sansodhan (editing) requests
    through Microsoft Foundry `gpt-6.0-luna` as the primary engine, with Azure OpenAI
    deployment fallback if configured.
    """

    def __init__(self):
        # Primary Azure AI / Microsoft Foundry settings (GPT-6.0-Luna)
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

        # Optional legacy Gemini fallback (disabled by default when Azure/GPT-6.0-Luna is configured)
        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
        self.fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()

        # If someone pasted their Azure/Foundry key into GEMINI_API_KEY by mistake, auto-route to Azure GPT-6.0-Luna
        provider_pref = os.getenv("AI_PROVIDER", "").strip().lower()
        if not self.azure_key and self.key:
            low_model = self.model.lower()
            if (
                provider_pref in {"azure", "foundry", "openai", "gpt", "luna"}
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

    def has_active_key(self) -> bool:
        return bool(self.azure_key or self.azure_openai_key or self.key)

    def _post_azure_chat(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> requests.Response:
        r = self._session.post(url, headers=headers, json=payload, timeout=self.timeout)
        if r.status_code == 400:
            err_low = r.text.lower()
            retry_payload = dict(payload)
            changed = False
            # Handle parameter differences across GPT-6.0-Luna / o-series / standard Azure OpenAI endpoints
            if "max_completion_tokens" in err_low and "max_completion_tokens" in retry_payload:
                tok_val = retry_payload.pop("max_completion_tokens")
                retry_payload["max_tokens"] = tok_val
                changed = True
            elif "max_tokens" in err_low and "max_tokens" in retry_payload:
                retry_payload.pop("max_tokens", None)
                changed = True
            if "temperature" in err_low and "temperature" in retry_payload:
                retry_payload.pop("temperature", None)
                changed = True
            if changed:
                r = self._session.post(url, headers=headers, json=retry_payload, timeout=self.timeout)
                if r.status_code == 400 and ("max_tokens" in r.text.lower() or "max_completion_tokens" in r.text.lower()):
                    retry_payload.pop("max_tokens", None)
                    retry_payload.pop("max_completion_tokens", None)
                    r = self._session.post(url, headers=headers, json=retry_payload, timeout=self.timeout)
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
        max_output_tokens: int | None = None,
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
        if max_output_tokens and int(max_output_tokens) > 0:
            payload["max_completion_tokens"] = int(max_output_tokens)

        headers = {
            "Content-Type": "application/json",
            "api-key": api_key,
            "Authorization": f"Bearer {api_key}",
        }

        base_host = _base_host_url(endpoint)
        candidate_urls = [endpoint]
        if "/openai/deployments/" not in endpoint:
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
                raise LunaError(f"Microsoft Foundry GPT-6.0-Luna network error ({model} at {cand_url}): {e}", retryable=True) from e

        if last_response is None or not last_response.ok:
            status_code = last_response.status_code if last_response is not None else 500
            err_text = last_response.text[:2000] if last_response is not None else "No response"
            retryable = status_code in _RETRYABLE_STATUS
            raise LunaError(
                f"Microsoft Foundry GPT-6.0-Luna HTTP {status_code} ({model}): {err_text}",
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
            raise LunaError(
                f"Microsoft Foundry GPT-6.0-Luna returned invalid structured JSON ({model}): {last_response.text[:2000]}"
            ) from e

        return _apply_schema_defaults(result, schema)

    def _call_gemini(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        thinking_level: str,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        gen: dict[str, Any] = {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _schema(schema),
        }
        if max_output_tokens and int(max_output_tokens) > 0:
            gen["maxOutputTokens"] = int(max_output_tokens)
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
            raise LunaError(f"Fallback API network error ({model}): {e}", retryable=True) from e

        if not r.ok:
            retryable = r.status_code in _RETRYABLE_STATUS
            raise LunaError(
                f"Fallback API HTTP {r.status_code} ({model}): {r.text[:2000]}",
                status_code=r.status_code,
                retryable=retryable,
            )
        try:
            body = r.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            result = _extract_json_object(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LunaError(f"Fallback API returned invalid structured JSON ({model}): {r.text[:2000]}") from e
        return _apply_schema_defaults(result, schema)

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        thinking_level: str = "medium",
        max_output_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate structured JSON powered by Microsoft Foundry `gpt-6.0-luna` as the primary engine:
        Tier 1: Primary Microsoft Foundry GPT-6.0-Luna (`gpt-6.0-luna`)
        Tier 2: Secondary Azure OpenAI Deployment (`gpt-4.1-mini`)
        Tier 3: Optional Gemini fallback ONLY if no Azure key is set (or ENABLE_GEMINI_FALLBACK=1)
        """
        if not self.azure_key and not self.azure_openai_key and not self.key:
            raise LunaError(
                "No AI API keys configured. Set AZURE_AI_KEY (for Microsoft Foundry gpt-6.0-luna) "
                "or AZURE_OPENAI_API_KEY."
            )

        level = str(thinking_level or "medium").lower()
        if level not in {"low", "medium", "high"}:
            level = "medium"

        errors: list[str] = []

        # Tier 1: Primary Azure AI / Microsoft Foundry (gpt-6.0-luna)
        if self.azure_key:
            try:
                return self._call_azure(
                    model=self.azure_model,
                    endpoint=self.azure_endpoint,
                    api_key=self.azure_key,
                    system=system,
                    prompt=prompt,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                )
            except LunaError as e:
                log.warning("Primary Microsoft Foundry model '%s' failed: %s. Attempting Azure fallback.", self.azure_model, e)
                errors.append(f"Microsoft Foundry ({self.azure_model}): {e}")

        # Tier 2: Secondary Azure OpenAI Deployment (gpt-4.1-mini) if different or if primary failed
        if self.azure_openai_key and (self.azure_openai_deployment != self.azure_model or not self.azure_key):
            try:
                log.info("Invoking secondary Azure OpenAI deployment: %s", self.azure_openai_deployment)
                return self._call_azure(
                    model=self.azure_openai_deployment,
                    endpoint=_normalize_azure_endpoint(self.azure_openai_endpoint),
                    api_key=self.azure_openai_key,
                    system=system,
                    prompt=prompt,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                )
            except LunaError as e:
                log.warning("Secondary Azure deployment '%s' failed: %s.", self.azure_openai_deployment, e)
                errors.append(f"Azure OpenAI ({self.azure_openai_deployment}): {e}")

        # Tier 3: Only use Gemini if NO Azure key is configured, or if ENABLE_GEMINI_FALLBACK=1 is explicitly set
        allow_gemini = (not self.azure_key and not self.azure_openai_key) or (
            os.getenv("ENABLE_GEMINI_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        if self.key and allow_gemini:
            models_to_try = [self.model]
            if self.fallback_model and self.fallback_model != self.model:
                models_to_try.append(self.fallback_model)

            for model in models_to_try:
                try:
                    return self._call_gemini(
                        model=model,
                        system=system,
                        prompt=prompt,
                        schema=schema,
                        thinking_level=level,
                        max_output_tokens=max_output_tokens,
                    )
                except LunaError as e:
                    errors.append(f"Fallback ({model}): {e}")
                    continue

        raise LunaError(
            "GPT-6.0-Luna / Azure AI request failed:\n" + "\n".join(f"- {err}" for err in errors)
        )


# Backwards-compatible alias so all existing `from .gemini_client import GeminiClient` calls use LunaClient
GeminiClient = LunaClient