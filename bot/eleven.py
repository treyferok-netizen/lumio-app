"""ElevenLabs с сервера Lumio: музыка (Music API) и голос (Text-to-Speech). Ключ — только на сервере.

lumio-set ELEVENLABS_API_KEY ключ   (в чат не присылать; read -s K; lumio-set ELEVENLABS_API_KEY "$K")
"""
import os

import requests

KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
BASE = os.environ.get("ELEVENLABS_BASE", "https://api.elevenlabs.io").rstrip("/")


def ready():
    return bool(KEY)


def _h():
    return {"xi-api-key": KEY, "Content-Type": "application/json"}


def _check(r):
    if r.status_code != 200:
        try:
            j = r.json()
            msg = j.get("detail") or j
        except ValueError:
            msg = r.text[:400]
        raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {str(msg)[:600]}")
    return r


def music(prompt=None, plan=None, length_ms=None, model_id="music_v1", instrumental=True):
    body = {"model_id": model_id}
    if plan:
        body["composition_plan"] = plan
    else:
        body["prompt"] = prompt
        if length_ms:
            body["music_length_ms"] = int(length_ms)
        body["force_instrumental"] = bool(instrumental)
    r = requests.post(BASE + "/v1/music?output_format=mp3_44100_192", json=body, headers=_h(), timeout=(15, 600))
    return _check(r).content


def tts(text, voice_id, model_id="eleven_v3", stability=0.5, similarity=0.75, style=0.0, speed=1.0):
    body = {"text": text, "model_id": model_id,
            "voice_settings": {"stability": stability, "similarity_boost": similarity, "style": style, "speed": speed}}
    r = requests.post(f"{BASE}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_192", json=body,
                      headers=_h(), timeout=(15, 300))
    return _check(r).content


def voices():
    r = requests.get(BASE + "/v2/voices?page_size=100", headers={"xi-api-key": KEY}, timeout=30)
    _check(r)
    return [{"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category")}
            for v in r.json().get("voices", [])]


def subscription():
    r = requests.get(BASE + "/v1/user/subscription", headers={"xi-api-key": KEY}, timeout=30)
    _check(r)
    j = r.json()
    return {k: j.get(k) for k in ("tier", "character_count", "character_limit", "next_character_count_reset_unix")}
