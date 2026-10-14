from __future__ import annotations
import base64, json, logging, os
import requests

from .http_retry import build_retrying_session
from .krutidev_converter import decode_krutidev_hybrid

log = logging.getLogger("legal-bot.speech")


class AzureSpeechError(RuntimeError):
    pass


_session = build_retrying_session(
    max_retries=int(os.getenv('AZURE_SPEECH_MAX_RETRIES', '3')),
    backoff_factor=float(os.getenv('AZURE_SPEECH_RETRY_BACKOFF_SECONDS', '1.5')),
    backoff_max=float(os.getenv('AZURE_SPEECH_RETRY_BACKOFF_MAX_SECONDS', '60')),
)

LEGAL_PHRASE_LIST = [
    'गाटा', 'खसरा', 'खतौनी', 'दाखिल खारिज', 'बैनामा', 'वादपत्र', 'वादी', 'वादीगण',
    'प्रतिवादी', 'प्रतिवादीगण', 'न्यायालय', 'तहसील', 'लेखपाल', 'नायब तहसीलदार',
    'स्थगनादेश', 'निषेधाज्ञा', 'अधिकार', 'कब्जा', 'सीमांकन', 'पैमाइश', 'रजिस्ट्री',
    'दाखिल-खारिज', 'प्रार्थनापत्र', 'शपथपत्र', 'साक्ष्य', 'प्रतिज्ञापत्र', 'मुकदमा',
    'वाद', 'अस्थायी निषेधाज्ञा', 'स्थायी निषेधाज्ञा', 'सिविल जज', 'जूनियर डिवीजन',
    'सीनियर डिवीजन', 'बिधूना', 'औरैया', 'अछल्दा', 'कुदरकोट', 'ऐरवा कटरा', 'एडवोकेट',
    'वी०डी० शुक्ला', 'बनाम', 'चौहद्दी', 'नजरी नक्शा', 'संक्रमणीय भूमिधर', 'मिनजानिव',
]


def _resolve_azure_endpoint() -> str:
    endpoint = os.getenv('AZURE_SPEECH_ENDPOINT', '').strip().rstrip('/')
    if endpoint:
        return endpoint
    region = os.getenv('AZURE_SPEECH_REGION', '').strip()
    if region:
        return f'https://{region}.api.cognitive.microsoft.com'
    return ''


def _mime_for(filename: str) -> str:
    low = (filename or '').lower()
    if low.endswith('.wav'):
        return 'audio/wav'
    if low.endswith('.mp3'):
        return 'audio/mpeg'
    if low.endswith('.flac'):
        return 'audio/flac'
    if low.endswith(('.m4a', '.mp4')):
        return 'audio/mp4'
    return 'audio/ogg'


def _transcribe_with_gemini(audio_bytes: bytes, mime: str) -> str:
    gemini_key = os.getenv('GEMINI_API_KEY', '').strip()
    if not gemini_key:
        raise AzureSpeechError('Neither Azure Speech credentials nor GEMINI_API_KEY are configured.')
    model = os.getenv('GEMINI_FALLBACK_MODEL', '') or os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model.strip()}:generateContent'
    prompt = (
        "Transcribe this Indian legal dictation audio verbatim in clean Unicode Devanagari Hindi "
        "(preserving any spoken English legal terms, numbers, dates, Gata/Khasra numbers, and names accurately). "
        "Return ONLY the raw transcript text with no commentary."
    )
    payload = {
        'contents': [{
            'role': 'user',
            'parts': [
                {'text': prompt},
                {'inlineData': {'mimeType': mime, 'data': base64.b64encode(audio_bytes).decode('ascii')}},
            ],
        }],
        'generationConfig': {'temperature': 0},
    }
    r = _session.post(url, params={'key': gemini_key}, json=payload, timeout=float(os.getenv('AZURE_SPEECH_TIMEOUT_SECONDS', '90')))
    if not r.ok:
        raise AzureSpeechError(f'Gemini audio transcription fallback HTTP {r.status_code}: {r.text[:1000]}')
    body = r.json()
    try:
        text = body['candidates'][0]['content']['parts'][0]['text'].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AzureSpeechError(f'Gemini audio fallback returned invalid payload: {body}') from exc
    if not text:
        raise AzureSpeechError('Gemini audio fallback returned an empty transcript.')
    return decode_krutidev_hybrid(text)


def transcribe_voice(audio_bytes: bytes, filename: str = 'telegram_voice.ogg') -> str:
    key = os.getenv('AZURE_SPEECH_KEY', '').strip()
    endpoint = _resolve_azure_endpoint()
    mime = _mime_for(filename)

    if not key or not endpoint:
        if os.getenv('GEMINI_API_KEY', '').strip():
            log.info('Azure Speech credentials not set; using Gemini multimodal audio transcription fallback.')
            return _transcribe_with_gemini(audio_bytes, mime)
        raise AzureSpeechError('AZURE_SPEECH_KEY and AZURE_SPEECH_ENDPOINT (or AZURE_SPEECH_REGION) are required.')

    locales_raw = os.getenv('AZURE_SPEECH_LOCALES', 'hi-IN,en-IN').strip()
    locales = [x.strip() for x in locales_raw.split(',') if x.strip()] or ['hi-IN', 'en-IN']
    definition = {
        'locales': locales,
        'enhancedMode': {'enabled': True, 'model': os.getenv('AZURE_SPEECH_MODEL', 'MAI-Transcribe-2')},
        'phraseList': {'phrases': LEGAL_PHRASE_LIST, 'biasingWeight': 1.6},
    }
    api_version = os.getenv('AZURE_SPEECH_API_VERSION', '2025-10-15').strip() or '2025-10-15'
    url = f'{endpoint}/speechtotext/transcriptions:transcribe?api-version={api_version}'
    files = {
        'audio': (filename, audio_bytes, mime),
        'definition': (None, json.dumps(definition, ensure_ascii=False), 'application/json'),
    }
    try:
        r = _session.post(
            url,
            headers={'Ocp-Apim-Subscription-Key': key},
            files=files,
            timeout=float(os.getenv('AZURE_SPEECH_TIMEOUT_SECONDS', '90')),
        )
    except requests.RequestException as e:
        if os.getenv('GEMINI_API_KEY', '').strip():
            log.warning('Azure Speech network error (%s); falling back to Gemini audio transcription.', e)
            return _transcribe_with_gemini(audio_bytes, mime)
        raise AzureSpeechError(f'Azure Speech network error: {e}') from e

    if not r.ok:
        if os.getenv('GEMINI_API_KEY', '').strip():
            log.warning('Azure Speech HTTP %s; falling back to Gemini audio transcription.', r.status_code)
            return _transcribe_with_gemini(audio_bytes, mime)
        raise AzureSpeechError(f'Azure Speech HTTP {r.status_code}: {r.text[:3000]}')

    try:
        body = r.json()
    except ValueError as e:
        raise AzureSpeechError('Azure Speech returned invalid JSON.') from e

    for key_name in ('combinedPhrases', 'phrases'):
        vals = body.get(key_name) or []
        text = ' '.join(str(x.get('text', '')).strip() for x in vals if isinstance(x, dict) and str(x.get('text', '')).strip()).strip()
        if text:
            return decode_krutidev_hybrid(text)
    raise AzureSpeechError(f'Azure Speech returned no transcript: {body}')
