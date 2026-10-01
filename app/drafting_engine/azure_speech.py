from __future__ import annotations
import json, os, shutil, subprocess, tempfile
from pathlib import Path
import requests

from .http_retry import RETRYABLE_STATUS as _RETRYABLE_STATUS
from .http_retry import build_retrying_session


class AzureSpeechError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


_session = build_retrying_session(
    max_retries=int(os.getenv('AZURE_SPEECH_MAX_RETRIES', '3')),
    backoff_factor=float(os.getenv('AZURE_SPEECH_RETRY_BACKOFF_SECONDS', '1.5')),
    backoff_max=float(os.getenv('AZURE_SPEECH_RETRY_BACKOFF_MAX_SECONDS', '60')),
)


def _resolve_endpoint() -> str:
    endpoint = os.getenv('AZURE_SPEECH_ENDPOINT', '').strip().rstrip('/')
    if endpoint:
        return endpoint
    region = os.getenv('AZURE_SPEECH_REGION', '').strip()
    if region:
        return f'https://{region}.api.cognitive.microsoft.com'
    raise AzureSpeechError('AZURE_SPEECH_ENDPOINT or AZURE_SPEECH_REGION is required.')


def _mime_for(filename: str) -> str:
    lower = str(filename or '').lower()
    if lower.endswith('.wav'):
        return 'audio/wav'
    if lower.endswith('.mp3'):
        return 'audio/mpeg'
    if lower.endswith('.flac'):
        return 'audio/flac'
    return 'audio/ogg'


def _convert_audio_with_ffmpeg(audio_bytes: bytes, filename: str) -> tuple[bytes, str]:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise AzureSpeechError('Azure rejected the source audio format and ffmpeg is not installed for conversion.')
    with tempfile.TemporaryDirectory(prefix='azure-speech-convert-') as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / (Path(filename or 'telegram_voice.ogg').name or 'telegram_voice.ogg')
        dst = tmp_dir / 'converted.wav'
        src.write_bytes(audio_bytes)
        command = [
            ffmpeg, '-y', '-i', str(src),
            '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
            str(dst),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=float(os.getenv('AZURE_SPEECH_FFMPEG_TIMEOUT_SECONDS', '45')),
            )
        except subprocess.TimeoutExpired as exc:
            raise AzureSpeechError('ffmpeg audio conversion timed out before Azure transcription.') from exc
        if completed.returncode != 0 or not dst.exists():
            details = (completed.stderr or completed.stdout or '').strip()[:1000]
            raise AzureSpeechError(f'ffmpeg audio conversion failed: {details}')
        return dst.read_bytes(), dst.name


def _transcribe_request(audio_bytes: bytes, filename: str) -> str:
    key = os.getenv('AZURE_SPEECH_KEY', '').strip()
    endpoint = _resolve_endpoint()
    if not key:
        raise AzureSpeechError('AZURE_SPEECH_KEY is required.')
    definition = {'enhancedMode': {'enabled': True, 'model': os.getenv('AZURE_SPEECH_MODEL', 'MAI-Transcribe-2')}, 'phraseList': {'phrases': [
        'गाटा', 'खसरा', 'खतौनी', 'दाखिल खारिज', 'बैनामा', 'वादपत्र', 'वादी', 'प्रतिवादी', 'न्यायालय', 'तहसील', 'लेखपाल', 'नायब तहसीलदार', 'स्थगनादेश', 'निषेधाज्ञा', 'अधिकार', 'कब्जा', 'सीमांकन', 'पैमाइश', 'रजिस्ट्री', 'दाखिल-खारिज', 'प्रार्थनापत्र', 'शपथपत्र', 'साक्ष्य', 'प्रतिज्ञापत्र', 'मुकदमा', 'वाद', 'अस्थायी निषेधाज्ञा', 'स्थायी निषेधाज्ञा', 'सिविल जज', 'बिधूना', 'औरैया', 'एडवोकेट', 'वी०डी० शुक्ला'
    ], 'biasingWeight': 1.6}}
    locale = os.getenv('AZURE_SPEECH_LOCALES', '').strip()
    if locale:
        definition['locales'] = [x.strip() for x in locale.split(',') if x.strip()]
    url = f'{endpoint}/speechtotext/transcriptions:transcribe?api-version={os.getenv("AZURE_SPEECH_API_VERSION", "2025-10-15")}'
    files = {
        'audio': (filename, audio_bytes, _mime_for(filename)),
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
        raise AzureSpeechError(f'Azure Speech network error: {e}', retryable=True) from e
    if not r.ok:
        retryable = r.status_code in _RETRYABLE_STATUS
        raise AzureSpeechError(
            f'Azure Speech HTTP {r.status_code}: {r.text[:3000]}',
            status_code=r.status_code,
            retryable=retryable,
        )
    try:
        body = r.json()
    except ValueError as e:
        raise AzureSpeechError('Azure Speech returned invalid JSON.') from e
    for key_name in ('combinedPhrases', 'phrases'):
        vals = body.get(key_name) or []
        text = ' '.join(str(x.get('text', '')).strip() for x in vals if isinstance(x, dict) and str(x.get('text', '')).strip()).strip()
        if text:
            return text
    raise AzureSpeechError(f'Azure Speech returned no transcript: {body}')


def transcribe_voice(audio_bytes: bytes, filename='telegram_voice.ogg') -> str:
    try:
        return _transcribe_request(audio_bytes, filename)
    except AzureSpeechError as exc:
        if exc.status_code not in {400, 415, 422} or str(filename).lower().endswith('.wav'):
            raise
        converted_bytes, converted_name = _convert_audio_with_ffmpeg(audio_bytes, filename)
        return _transcribe_request(converted_bytes, converted_name)
