# Reliability: fixing "503 / model overloaded" errors

## Root cause
`app/drafting_engine/gemini_client.py` called the Gemini `generateContent`
REST API with a plain `requests.post(...)` and **no retry logic**. Google's
own troubleshooting guide states that `429` and `503` are expected, transient
conditions during high demand, and recommends exponential-backoff retries —
their own SDKs retry automatically up to 4 times.
(https://ai.google.dev/gemini-api/docs/troubleshooting)

Because this bot had zero retries, a single transient `503 UNAVAILABLE` from
Google instantly failed the whole draft (`draft_live()` calls Gemini 2–3
times per document: structure planning + up to 2 drafting attempts), and the
user saw a generic "Draft generate नहीं हो सका" message.

## What changed
- `app/drafting_engine/http_retry.py` — a shared `requests.Session` builder
  whose `HTTPAdapter` retries `408/429/500/502/503/504` with exponential
  backoff + jitter (respecting `Retry-After` when Google sends it) before any
  exception reaches application code.
- `app/drafting_engine/gemini_client.py` — uses that session, and after
  exhausting retries on `GEMINI_MODEL`, automatically falls back once to
  `GEMINI_FALLBACK_MODEL` (default `gemini-2.5-flash`) instead of failing
  outright. Errors now carry a `status_code` and `retryable` flag.
- `app/drafting_engine/azure_speech.py` — given the same retrying session,
  since Azure Speech is exposed to the same class of transient failures.
- `app/bot.py` — distinguishes a transient overload (`GeminiError.retryable`)
  from a hard configuration error and tells the user to simply retry in a
  minute, vs. telling them (or your admin) to check `GEMINI_API_KEY` /
  `GEMINI_MODEL`.

## New environment variables (see `.env.example`)
`GEMINI_FALLBACK_MODEL`, `GEMINI_MAX_RETRIES`, `GEMINI_RETRY_BACKOFF_SECONDS`,
`GEMINI_RETRY_BACKOFF_MAX_SECONDS`, and the Azure Speech equivalents. Existing
`GEMINI_MODEL`, `GEMINI_TIMEOUT_SECONDS`, `GEMINI_*_THINKING_LEVEL`,
`CORPUS_*` variables were previously used by the code but were missing from
`.env.example` — set them in your Heroku config vars.

## Additional levers if 503s persist
1. **Lower thinking level for drafting.** `GEMINI_DRAFTING_THINKING_LEVEL=high`
   plus a ~90KB retrieval context (`CORPUS_CONTEXT_MAX_CHARS`) is a large,
   slow request — large/slow requests are statistically more likely to land
   on an overloaded backend. Try `medium` first; compare draft quality.
2. **Shrink `CORPUS_CONTEXT_MAX_CHARS`** (e.g. `40000`) and/or
   `CORPUS_TOP_K` (e.g. `4-5`) — smaller prompts are faster and cheaper, and
   the retriever already ranks the most relevant example drafts first.
3. **Use a paid/billed Gemini API key**, not a free-tier AI Studio key. Free
   tier keys share a much smaller capacity pool and hit `503`/`429` far more
   often during peak hours.
4. **Heroku dyno type**: `Procfile` runs `worker: python -m app.bot`
   (long-polling), which is correct — it is *not* subject to the 30s HTTP
   request timeout that affects `web` dynos, so it is not itself a cause of
   your errors. Make sure the worker dyno is actually scaled to `1`
   (`heroku ps:scale worker=1`) and check `heroku logs --tail --dyno=worker`
   for `GeminiError` lines to see live status codes and confirm the fallback
   is being used.
5. Watch for Google status-page incidents — sustained regional overload spikes
   are visible at https://status.cloud.google.com/ under "Vertex AI /
   Generative Language API".
