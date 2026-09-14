from __future__ import annotations
import json, logging, os
from typing import Any

import requests

from .http_retry import RETRYABLE_STATUS as _RETRYABLE_STATUS
from .http_retry import build_retrying_session

log = logging.getLogger("legal-bot.gemini")


class GeminiError(RuntimeError):
    """Raised for any failed Gemini call.

    status_code is None for network-level failures (DNS/timeout/connection reset).
    retryable tells callers whether this failure is a transient overload/rate-limit
    condition (already retried internally) vs. a hard failure (bad key, bad schema,
    invalid model, etc.) that will not be helped by retrying again.
    """

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _schema(s: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(s, dict):
        return s
    out = {}
    m = {'object': 'OBJECT', 'array': 'ARRAY', 'string': 'STRING', 'integer': 'INTEGER', 'number': 'NUMBER', 'boolean': 'BOOLEAN'}
    if s.get('type'):
        out['type'] = m.get(str(s['type']).lower(), str(s['type']).upper())
    if 'properties' in s:
        out['properties'] = {k: _schema(v) for k, v in s['properties'].items()}
    if 'items' in s:
        out['items'] = _schema(s['items'])
    for k in ('required', 'enum', 'description', 'minItems', 'maxItems', 'minimum', 'maximum'):
        if k in s:
            out[k] = s[k]
    return out


class GeminiClient:
    def __init__(self):
        self.key = os.getenv('GEMINI_API_KEY', '').strip()
        self.model = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash').strip()
        # Used automatically as a last resort if the primary model keeps
        # returning transient errors (overloaded / rate-limited) after all
        # retries. Set GEMINI_FALLBACK_MODEL="" to disable.
        self.fallback_model = os.getenv('GEMINI_FALLBACK_MODEL', 'gemini-3.7-flash').strip()
        self.timeout = float(os.getenv('GEMINI_TIMEOUT_SECONDS', '90'))
        self.max_retries = int(os.getenv('GEMINI_MAX_RETRIES', '3'))
        self.retry_backoff_seconds = float(os.getenv('GEMINI_RETRY_BACKOFF_SECONDS', '1.5'))
        self.retry_backoff_max_seconds = float(os.getenv('GEMINI_RETRY_BACKOFF_MAX_SECONDS', '60'))
        if not self.key:
            raise GeminiError('GEMINI_API_KEY is not set.')
        self._session = build_retrying_session(
            max_retries=self.max_retries,
            backoff_factor=self.retry_backoff_seconds,
            backoff_max=self.retry_backoff_max_seconds,
        )

    def _call(self, *, model: str, system: str, prompt: str, schema: dict[str, Any], thinking_level: str) -> dict[str, Any]:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        gen = {'temperature': 0, 'responseMimeType': 'application/json', 'responseSchema': _schema(schema)}
        gen['thinkingConfig'] = {'thinkingLevel': thinking_level}
        payload = {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
            'generationConfig': gen,
        }
        try:
            r = self._session.post(url, params={'key': self.key}, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise GeminiError(f'Gemini network error ({model}): {e}', retryable=True) from e

        if not r.ok:
            retryable = r.status_code in _RETRYABLE_STATUS
            raise GeminiError(
                f'Gemini HTTP {r.status_code} ({model}) after up to {self.max_retries + 1} attempt(s): {r.text[:2000]}',
                status_code=r.status_code,
                retryable=retryable,
            )
        try:
            body = r.json()
            text = body['candidates'][0]['content']['parts'][0]['text']
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise GeminiError(f'Gemini returned invalid structured JSON ({model}): {r.text[:2000]}') from e
        if not isinstance(result, dict):
            raise GeminiError(f'Gemini response was not a JSON object ({model}).')
        return result

    def generate_json(self, *, system: str, prompt: str, schema: dict[str, Any], thinking_level='medium') -> dict[str, Any]:
        level = str(thinking_level or 'medium').lower()
        if level not in {'low', 'medium', 'high'}:
            level = 'medium'

        models_to_try = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models_to_try.append(self.fallback_model)

        last_err: GeminiError | None = None
        for i, model in enumerate(models_to_try):
            try:
                if i > 0:
                    log.warning("Gemini model '%s' kept failing (%s); falling back to '%s'.", self.model, last_err, model)
                return self._call(model=model, system=system, prompt=prompt, schema=schema, thinking_level=level)
            except GeminiError as e:
                last_err = e
                log.warning("Gemini call failed on model '%s': %s", model, e)
                if not e.retryable:
                    # Non-transient failure (bad key/schema/model id) — trying
                    # the fallback model won't help unless the model id itself
                    # was the problem, but it's cheap to try once more.
                    continue
                continue
        assert last_err is not None
        raise last_err
