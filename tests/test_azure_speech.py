from __future__ import annotations

import pytest

from app.drafting_engine import azure_speech


class _Resp:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._body


def test_region_endpoint_is_derived_when_endpoint_missing(monkeypatch):
    calls = []

    class _Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return _Resp(200, {"combinedPhrases": [{"text": "नमूना ट्रांसक्रिप्ट"}]})

    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "centralindia")
    monkeypatch.delenv("AZURE_SPEECH_ENDPOINT", raising=False)
    monkeypatch.setattr(azure_speech, "_session", _Session())

    text = azure_speech.transcribe_voice(b"audio", "telegram_voice.ogg")

    assert text == "नमूना ट्रांसक्रिप्ट"
    assert calls[0][0].startswith("https://centralindia.api.cognitive.microsoft.com/")


def test_unsupported_audio_falls_back_to_ffmpeg_wav(monkeypatch):
    calls = []

    class _Session:
        def __init__(self):
            self.count = 0

        def post(self, url, **kwargs):
            self.count += 1
            calls.append(kwargs)
            if self.count == 1:
                return _Resp(415, text="unsupported format")
            return _Resp(200, {"combinedPhrases": [{"text": "ok"}]})

    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://speech.example.com")
    monkeypatch.setattr(azure_speech, "_session", _Session())
    monkeypatch.setattr(azure_speech, "_convert_audio_with_ffmpeg", lambda *_: (b"wav", "converted.wav"))

    text = azure_speech.transcribe_voice(b"audio", "telegram_voice.ogg")

    assert text == "ok"
    assert calls[0]["files"]["audio"][0] == "telegram_voice.ogg"
    assert calls[1]["files"]["audio"][0] == "converted.wav"


def test_non_format_error_does_not_attempt_ffmpeg(monkeypatch):
    class _Session:
        def post(self, url, **kwargs):
            return _Resp(503, text="unavailable")

    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://speech.example.com")
    monkeypatch.setattr(azure_speech, "_session", _Session())
    monkeypatch.setattr(
        azure_speech,
        "_convert_audio_with_ffmpeg",
        lambda *_: (_ for _ in ()).throw(AssertionError("ffmpeg fallback should not run")),
    )

    with pytest.raises(azure_speech.AzureSpeechError) as err:
        azure_speech.transcribe_voice(b"audio", "telegram_voice.ogg")
    assert err.value.status_code == 503
    assert err.value.retryable is True
