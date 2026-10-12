"""Bounded WAV transcription through Bailian. No audio/keys/provider errors are logged."""
from __future__ import annotations

import base64
import io
import math
import os
import re
import struct
import wave
from urllib.parse import urlparse

import httpx

MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
MAX_TRANSCRIPT_CHARS = 2000
DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class SpeechError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def intent_text(transcript: str) -> str:
    # Correct only exact read-only query homophones. Never rewrite a title/date/time.
    normalized = re.sub(r"[\s，,。.!！?？]", "", transcript)
    if normalized in {"查询代办", "查看代办", "我的代办", "代办查询"}:
        return "查询待办"
    return transcript


def validate_audio(encoded: str) -> bytes:
    try:
        data = base64.b64decode(encoded, validate=True)
        if not 44 <= len(data) <= MAX_AUDIO_BYTES or not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
            raise ValueError()
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate, frames = audio.getframerate(), audio.getnframes()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE"
                    or not 8000 <= rate <= 48000 or not 0 < frames / rate <= MAX_AUDIO_SECONDS):
                raise ValueError()
            samples = audio.readframes(frames)
            if len(samples) != frames * 2:
                raise ValueError()
        # Silent audio must not be allowed to hallucinate a task.
        energy = sum(sample[0] ** 2 for sample in struct.iter_unpack("<h", samples))
        if math.sqrt(energy / frames) < 60:
            raise SpeechError("speech_silent")
        return data
    except SpeechError:
        raise
    except (ValueError, TypeError, wave.Error, EOFError, struct.error):
        raise SpeechError("speech_invalid_audio") from None


def configuration():
    if os.getenv("ASR_ENABLED", "false").lower() != "true":
        raise SpeechError("speech_disabled")
    base = os.getenv("ASR_BASE_URL", DEFAULT_BASE).rstrip("/")
    try:
        url = urlparse(base)
        port = url.port
    except ValueError:
        raise SpeechError("speech_bad_configuration") from None
    hosts = {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
    if (url.scheme != "https" or url.hostname not in hosts or url.username or url.password or
            port not in (None, 443) or url.path != "/compatible-mode/v1" or url.query or url.fragment):
        raise SpeechError("speech_bad_configuration")
    key = os.getenv("ASR_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("ALIYUN_API_KEY")
    # Never send a different vendor's model key to Bailian.
    if not key and os.getenv("MODEL_BASE_URL", "").rstrip("/") == base:
        key = os.getenv("MODEL_API_KEY")
    if not key:
        raise SpeechError("speech_not_configured")
    model = os.getenv("ASR_MODEL", "qwen3-asr-flash")
    if not re.fullmatch(r"qwen3-asr-flash(?:-\d{4}-\d{2}-\d{2})?", model):
        raise SpeechError("speech_bad_configuration")
    return base, key, model


def transcribe(data: bytes) -> str:
    base, key, model = configuration()
    payload = {"model": model, "messages": [{"role": "user", "content": [{
        "type": "input_audio", "input_audio": {
            "data": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")}}]}],
        "stream": False, "asr_options": {"enable_itn": False}}
    try:
        with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
            with client.stream("POST", base + "/chat/completions", json=payload,
                               headers={"Authorization": "Bearer " + key}) as response:
                if response.status_code != 200:
                    raise SpeechError("speech_provider_failed")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 32768:
                        raise SpeechError("speech_invalid_result")
                import json
                result = json.loads(raw)
        text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TRANSCRIPT_CHARS:
            raise SpeechError("speech_invalid_result")
        return text.strip()
    except SpeechError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        raise SpeechError("speech_unavailable") from None
