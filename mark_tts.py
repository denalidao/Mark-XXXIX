"""
Text-to-speech for **local Ollama** reply paths.

1. **Coqui / TechGym TTS** (optional): ``tts_backend`` ``coqui`` — loads your local
   clone via ``coqui_tts_repo_path``; see ``mark_coqui_tts.py``. On failure, optionally
   **Gemini TTS** if ``coqui_failover_to_gemini`` is true and a key is set; else **Windows SAPI**.
2. **Gemini TTS** (optional): ``tts_backend`` ``gemini`` + ``gemini_api_key``;
   audio via **sounddevice**. On failure, **Windows SAPI**.
3. **pyttsx3 / SAPI** (``pyttsx3``): Windows installed voices; also the **fallback**
   for Coqui and Gemini.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import io
import os
import tempfile
import platform
import sys
import threading
import wave

_tls = threading.local()
# pyttsx3 / SAPI are not safe across thread-pool threads; serialize all TTS.
_tts_lock = threading.Lock()
_tts_backend_hint_printed = False
_gemini_latency_hint_printed = False


def _invoke_on_audio_start(cb: Callable[[], None] | None) -> None:
    """Notify HUD / callers right before audio leaves the speaker (best-effort)."""
    if not cb:
        return
    try:
        cb()
    except Exception as ex:
        print(f"[TTS] on_audio_start callback failed: {ex}")


def _ensure_win32_com_apartment() -> None:
    """SAPI/pyttsx3 often runs from a thread pool; COM must be initialized per thread."""
    if sys.platform != "win32":
        return
    if getattr(_tls, "mark_pyttsx3_com", False):
        return
    try:
        import pythoncom  # type: ignore[import-untyped]
    except ImportError:
        _tls.mark_pyttsx3_com = True
        return
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    except Exception:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
    _tls.mark_pyttsx3_com = True


def create_pyttsx3_engine():
    import pyttsx3

    if platform.system() == "Windows":
        for driver in ("sapi5", None):
            try:
                return pyttsx3.init(driver) if driver else pyttsx3.init()
            except Exception:
                continue
    return pyttsx3.init()


def _apply_pyttsx3_voice_substring(engine, substring: str | None) -> None:
    if not substring or not substring.strip():
        return
    needle = substring.strip().lower()
    try:
        voices = engine.getProperty("voices")
    except Exception:
        voices = None
    if not voices:
        return
    for v in voices:
        name = getattr(v, "name", "") or ""
        vid = getattr(v, "id", None)
        if not vid:
            continue
        if needle in name.lower():
            try:
                engine.setProperty("voice", vid)
                print(f"[TTS] Voice: {name!r}")
            except Exception as ex:
                print(f"[TTS] Could not set voice {name!r}: {ex}")
            return
    print(
        f"[TTS] No SAPI voice contains {substring!r}. "
        "List names: python -c \"import pyttsx3; e=pyttsx3.init(); "
        "print([getattr(x,'name',x) for x in (e.getProperty('voices') or [])])\""
    )


def configure_pyttsx3(engine) -> None:
    try:
        engine.setProperty("volume", 1.0)
    except Exception:
        pass
    try:
        rate = engine.getProperty("rate")
        if rate is not None and rate > 260:
            engine.setProperty("rate", 220)
    except Exception:
        pass
    try:
        from mark_llm_settings import get_local_tts_voice_substring

        _apply_pyttsx3_voice_substring(engine, get_local_tts_voice_substring())
    except Exception as ex:
        print(f"[TTS] Voice config skipped: {ex}")


def _speak_pyttsx3_no_lock(
    text: str,
    *,
    on_audio_start: Callable[[], None] | None = None,
) -> None:
    """
    ``pyttsx3`` / SAPI path; caller must hold ``_tts_lock``.

    When possible, render to a temp WAV and play via **sounddevice** — same output
    path as Gemini TTS — so Bluetooth / app default routing matches neural speech.
    If ``save_to_file`` fails, fall back to ``say`` + ``runAndWait()`` (Windows
    default SAPI device only).
    """
    utter = (text or "").strip()
    if not utter:
        return
    _ensure_win32_com_apartment()

    tmp_path: str | None = None
    try:
        eng = create_pyttsx3_engine()
        configure_pyttsx3(eng)
        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        eng.save_to_file(utter, tmp_path)
        eng.runAndWait()
        with open(tmp_path, "rb") as wf:
            data = wf.read()
        if data.startswith(b"RIFF") and len(data) > 100:
            _invoke_on_audio_start(on_audio_start)
            _play_wav_bytes_riff(data)
            return
        print("[TTS] SAPI WAV export was empty or not RIFF — using live SAPI output.")
    except Exception as ex:
        print(f"[TTS] SAPI→PortAudio bridge failed ({ex}); using live SAPI output.")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    eng2 = create_pyttsx3_engine()
    configure_pyttsx3(eng2)
    eng2.say(utter)
    _invoke_on_audio_start(on_audio_start)
    eng2.runAndWait()


def _play_pcm_int16_mono(pcm: bytes, sample_rate: int) -> None:
    import numpy as np
    import sounddevice as sd

    audio = np.frombuffer(pcm, dtype=np.int16)
    sd.play(audio, sample_rate, blocking=True)


def _play_wav_bytes_riff(wav_bytes: bytes) -> None:
    """Play RIFF WAV via PortAudio."""
    import numpy as np
    import sounddevice as sd

    bio = io.BytesIO(wav_bytes)
    with wave.open(bio, "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw == 1:
        audio = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
        audio = (audio.astype(np.int32) * 256).astype(np.int16)
    elif sw == 2:
        audio = np.frombuffer(raw, dtype=np.int16)
    elif sw == 4:
        audio = (np.frombuffer(raw, dtype=np.int32) / 65536.0).astype(np.int16)
    else:
        raise ValueError(f"Unsupported WAV sample width: {sw}")
    if nch > 1:
        audio = audio.reshape(-1, nch).mean(axis=1).astype(np.int16)
    sd.play(audio, sr, blocking=True)


def _normalize_audio_blob_data(raw) -> bytes | None:
    """API may return ``bytes``, ``memoryview``, or **base64 ``str``** (per REST docs)."""
    if raw is None:
        return None
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if isinstance(raw, bytes):
        return raw if raw else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            dec = base64.b64decode(s, validate=False)
        except Exception:
            return None
        return dec if dec else None
    return None


def _gemini_extract_audio(response) -> tuple[bytes, str] | None:
    """Return ``(pcm_or_wav_bytes, mime_type_lower)`` from a ``generate_content`` response."""
    cands = getattr(response, "candidates", None) or []
    audio_parts: list[tuple[bytes, str]] = []
    for cand in cands:
        content = getattr(cand, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None) or []
        for p in parts:
            inl = getattr(p, "inline_data", None)
            if not inl:
                continue
            mime = (getattr(inl, "mime_type", None) or "").lower()
            blob = _normalize_audio_blob_data(getattr(inl, "data", None))
            if not blob:
                continue
            if "audio" in mime or mime.endswith("/wav") or mime.endswith("/x-wav"):
                audio_parts.append((blob, mime))
            elif not mime and blob[:4] == b"RIFF":
                audio_parts.append((blob, "audio/wav"))
            elif not mime:
                audio_parts.append((blob, "audio/pcm"))
    if audio_parts:
        for blob, mime in audio_parts:
            if "wav" in mime or blob[:4] == b"RIFF":
                return blob, mime
        return audio_parts[0][0], audio_parts[0][1]
    # Fallback: first inline blob (some responses omit audio/* in mime_type)
    for cand in cands:
        content = getattr(cand, "content", None)
        if not content:
            continue
        for p in getattr(content, "parts", None) or []:
            inl = getattr(p, "inline_data", None)
            if not inl:
                continue
            blob = _normalize_audio_blob_data(getattr(inl, "data", None))
            if blob:
                mime = (getattr(inl, "mime_type", None) or "").lower()
                return blob, mime or "audio/pcm"
    return None


# After the configured primary, try these on 429 (deduped, order preserved).
_GEMINI_TTS_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash-preview-tts",
    "gemini-3.1-flash-tts-preview",
)


def _gemini_tts_models_to_try(primary: str) -> list[str]:
    """Primary first, then other known TTS model ids (no duplicates)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in (primary, *_GEMINI_TTS_FALLBACK_MODELS):
        mid = (m or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _is_gemini_tts_quota_error(exc: BaseException) -> bool:
    s = str(exc)
    return (
        "429" in s
        or "RESOURCE_EXHAUSTED" in s
        or "Quota exceeded" in s
        or "quota" in s.lower()
    )


def _is_gemini_tts_transient_or_quota(exc: BaseException) -> bool:
    """429 / quota plus common overload and gateway errors — try next model or SAPI."""
    if _is_gemini_tts_quota_error(exc):
        return True
    s = str(exc).lower()
    return any(
        needle in s
        for needle in (
            "503",
            "502",
            "500",
            "unavailable",
            "overloaded",
            "timeout",
            "deadline",
            "temporarily unavailable",
            "try again",
            "connection reset",
            "connection aborted",
            "internal error",
        )
    )


def _try_gemini_tts(
    text: str,
    *,
    on_audio_start: Callable[[], None] | None = None,
    require_gemini_backend: bool = True,
) -> bool:
    """
    Synthesize with Gemini TTS API and play. Returns False to fall back to pyttsx3.

    When ``require_gemini_backend`` is True (default), only runs if ``tts_backend`` is
    ``gemini`` — the normal primary path. Set False for **Coqui failover** so
    Gemini can run while the configured backend stays local.
    """
    from mark_llm_settings import (
        get_gemini_api_key,
        get_gemini_live_voice_name,
        get_gemini_tts_model,
        get_local_tts_backend,
    )

    if require_gemini_backend and get_local_tts_backend() != "gemini":
        ev = os.environ.get("MARK_TTS_BACKEND", "").strip()
        if ev:
            print(
                f"[TTS] MARK_TTS_BACKEND={ev!r} is set — it overrides api_keys.json. "
                "Unset it to use the UI / file ``tts_backend: gemini`` setting."
            )
        return False
    key = get_gemini_api_key()
    if not key:
        print(
            "[TTS] Gemini voice selected but ``gemini_api_key`` is empty in api_keys.json."
        )
        return False
    utter = (text or "").strip()
    if not utter:
        return False
    try:
        from google import genai
        from google.genai import types
    except ImportError as ex:
        print(f"[TTS] google-genai import failed: {ex}")
        return False
    primary = get_gemini_tts_model()
    voice = get_gemini_live_voice_name()
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})

    models_to_try = _gemini_tts_models_to_try(primary)

    response = None
    model_used = primary
    got: tuple[bytes, str] | None = None

    for i, model in enumerate(models_to_try):
        try:
            response = client.models.generate_content(
                model=model,
                contents=utter,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice,
                            )
                        ),
                    ),
                ),
            )
            model_used = model
        except Exception as ex:
            transient = _is_gemini_tts_transient_or_quota(ex)
            if transient and i + 1 < len(models_to_try):
                nxt = models_to_try[i + 1]
                short = str(ex).strip().replace("\n", " ")[:160]
                print(
                    f"[TTS] Gemini TTS model {model!r} failed ({short}); retrying {nxt!r}…"
                )
                continue
            if transient:
                print(
                    "[TTS] Gemini TTS: all listed cloud models failed (quota / overload / "
                    "gateway). Falling back to Windows SAPI. "
                    "https://ai.google.dev/gemini-api/docs/rate-limits"
                )
            else:
                print(f"[TTS] Gemini TTS request failed: {ex}")
            return False

        got = _gemini_extract_audio(response)
        if got:
            break

        cands = getattr(response, "candidates", None) or []
        fr = None
        if cands:
            fr = getattr(cands[0], "finish_reason", None)
        if i + 1 < len(models_to_try):
            nxt = models_to_try[i + 1]
            print(
                f"[TTS] Gemini TTS model {model!r} returned no usable audio "
                f"(finish_reason={fr!r}); retrying {nxt!r}…"
            )
            continue

        if not cands:
            print("[TTS] Gemini TTS: empty candidates (prompt blocked or model error).")
        else:
            print(
                f"[TTS] Gemini TTS returned no audio parts "
                f"(finish_reason={fr!r}). Check model id ``{model_used}`` and API quotas."
            )
        return False

    if response is None or not got:
        return False
    data, mime = got
    try:
        # Serialize PortAudio / SAPI with pyttsx3; do not hold this lock during HTTP above
        # (avoids vision thread blocking forever behind chat TTS, and vice versa).
        with _tts_lock:
            _invoke_on_audio_start(on_audio_start)
            if "wav" in mime or data[:4] == b"RIFF":
                _play_wav_bytes_riff(data)
            else:
                # Default PCM from Gemini TTS docs: 24 kHz mono int16
                rate = 24000
                if "24000" in mime:
                    rate = 24000
                elif "48000" in mime:
                    rate = 48000
                _play_pcm_int16_mono(data, rate)
    except Exception as ex:
        print(f"[TTS] Gemini audio playback failed: {ex}")
        return False
    print(f"[TTS] Gemini TTS voice={voice!r} model={model_used!r}")
    return True


def speak_local_tts(
    text: str,
    *,
    on_audio_start: Callable[[], None] | None = None,
) -> None:
    """``pyttsx3`` only (Windows SAPI). Serialized with other TTS paths."""
    if not (text or "").strip():
        return
    with _tts_lock:
        _speak_pyttsx3_no_lock(text, on_audio_start=on_audio_start)


def speak_mark_tts(
    text: str,
    *,
    on_audio_start: Callable[[], None] | None = None,
) -> None:
    """
    Local Jarvis speech (Ollama path): **Coqui** (``tts_backend: coqui``),
    **Gemini TTS** (``gemini`` + API key), or **Windows SAPI** (``pyttsx3``).
    Coqui and Gemini failures always fall back to **pyttsx3** so you still hear
    output while tuning local TTS.

    ``on_audio_start`` runs on the TTS thread **immediately before** PortAudio /
    SAPI begins playback — use it to sync the SPEAKING HUD with audible output
    (Gemini otherwise spends seconds in HTTP synthesis first).

    **Coqui + cloud:** When ``tts_backend`` is ``coqui``, Gemini TTS runs after a
    Coqui miss only if ``coqui_failover_to_gemini`` is true (UI checkbox or JSON)
    **and** ``gemini_api_key`` is set — you choose the chain; keys are not implied.
    """
    if not (text or "").strip():
        return
    global _tts_backend_hint_printed, _gemini_latency_hint_printed
    from mark_llm_settings import (
        get_coqui_failover_to_gemini,
        get_gemini_api_key,
        get_local_tts_backend,
    )

    with _tts_lock:
        if (
            not _tts_backend_hint_printed
            and get_local_tts_backend() == "pyttsx3"
            and get_gemini_api_key()
        ):
            print(
                "[TTS] You have gemini_api_key but tts_backend is \"pyttsx3\" (Windows SAPI) — "
                "replies use Zira (or your SAPI pick). In the UI under VOICE OUTPUT "
                "(LOCAL), choose \"Gemini neural (uses API key)\", or set "
                "\"tts_backend\": \"gemini\" in config/api_keys.json."
            )
            _tts_backend_hint_printed = True

    backend = get_local_tts_backend()

    if backend == "coqui":
        from mark_coqui_tts import try_speak_coqui

        if try_speak_coqui(text, on_audio_start=on_audio_start, tts_lock=_tts_lock):
            return
        if get_coqui_failover_to_gemini() and get_gemini_api_key():
            print(
                "[TTS] Coqui did not speak — trying Gemini TTS (``coqui_failover_to_gemini`` on)."
            )
            if _try_gemini_tts(
                text,
                on_audio_start=on_audio_start,
                require_gemini_backend=False,
            ):
                with _tts_lock:
                    if not _gemini_latency_hint_printed:
                        print(
                            "[TTS] Latency: Gemini TTS calls the cloud for every reply (often 1–4s). "
                            "For fastest local speech set \"tts_backend\": \"sapi\" in api_keys.json "
                            "or env MARK_TTS_BACKEND=sapi (Windows SAPI / pyttsx3)."
                        )
                        _gemini_latency_hint_printed = True
                return

    # Gemini HTTP runs without holding _tts_lock so vision + chat threads do not deadlock.
    if backend == "gemini" and get_gemini_api_key():
        if _try_gemini_tts(text, on_audio_start=on_audio_start):
            with _tts_lock:
                if not _gemini_latency_hint_printed:
                    print(
                        "[TTS] Latency: Gemini TTS calls the cloud for every reply (often 1–4s). "
                        "For fastest local speech set \"tts_backend\": \"sapi\" in api_keys.json "
                        "or env MARK_TTS_BACKEND=sapi (Windows SAPI / pyttsx3)."
                    )
                    _gemini_latency_hint_printed = True
            return

    with _tts_lock:
        if backend == "coqui":
            fo = get_coqui_failover_to_gemini()
            key = bool(get_gemini_api_key())
            if fo and key:
                tail = " Coqui and Gemini TTS both failed — using Windows SAPI."
            elif fo and not key:
                tail = (
                    " Failover to Gemini is on but there is no ``gemini_api_key`` — "
                    "using Windows SAPI."
                )
            elif not fo and key:
                tail = (
                    " Gemini failover is off (``coqui_failover_to_gemini``: false) — "
                    "using Windows SAPI."
                )
            else:
                tail = " No Gemini key and failover off — using Windows SAPI."
            print(
                "[TTS] Coqui unavailable for this reply."
                + tail
                + " (If load failed, the checklist block above lists what to fix.)"
            )
        elif backend == "gemini":
            print(
                "[TTS] Gemini TTS failed or returned no usable audio — "
                "falling back to Windows SAPI (e.g. Zira). See [TTS] lines above."
            )
        print("[TTS] Invoking Windows SAPI (pyttsx3) fallback now…")
        try:
            _speak_pyttsx3_no_lock(text, on_audio_start=on_audio_start)
            print("[TTS] Windows SAPI fallback completed.")
        except Exception as ex:
            print(f"[TTS] Windows SAPI fallback failed: {ex}")
            raise
