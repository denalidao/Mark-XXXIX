"""LLM backend selection: Gemini (cloud) or Ollama (local), aligned with Aletheon env conventions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


API_CONFIG_PATH = get_base_dir() / "config" / "api_keys.json"

_config_load_error_printed = False
_coqui_cuda_cpu_note_printed = False


def _load_config() -> dict:
    global _config_load_error_printed
    if not API_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as ex:
        if not _config_load_error_printed:
            print(
                f"[config] api_keys.json is not valid JSON — settings will be empty until fixed: "
                f"{API_CONFIG_PATH}\n[config] {type(ex).__name__}: {ex}"
            )
            _config_load_error_printed = True
        return {}
    except OSError as ex:
        if not _config_load_error_printed:
            print(f"[config] Cannot read api_keys.json: {API_CONFIG_PATH}\n[config] {ex}")
            _config_load_error_printed = True
        return {}


def coqui_settings_debug_snippet() -> str:
    """
    One-line hint when Coqui path looks empty but you edited ``api_keys.json`` —
    shows which file was read, env override, and raw JSON value (no other secrets).
    """
    data = _load_config()
    coqui_keys = sorted(k for k in data if str(k).lower().startswith("coqui"))
    raw = data.get("coqui_tts_repo_path")
    env = os.environ.get("MARK_COQUI_REPO")
    env_note = "unset" if env is None else repr(env)
    return (
        f"Read api_keys from: {API_CONFIG_PATH} (exists={API_CONFIG_PATH.exists()}); "
        f"``coqui_*`` keys in file: {coqui_keys}; "
        f"``coqui_tts_repo_path`` raw in JSON: {raw!r}; "
        f"env MARK_COQUI_REPO: {env_note} (env wins over file when non-empty after strip)"
    )


def _normalize_ollama_base(url: str) -> str:
    """
    Ollama native calls use ``{base}/api/chat``. If ``ollama_url`` was pasted with
    ``/api`` or OpenAI-style ``/v1`` suffixes, strip them so we do not POST to
    the wrong path (often surfaces as HTTP 405).
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return "http://127.0.0.1:11434"
    lower = u.lower()
    for suf in (
        "/v1/chat/completions",
        "/v1/chat",
        "/api/chat",
        "/api/generate",
        "/api/tags",
        "/api",
        "/v1",
    ):
        if lower.endswith(suf):
            u = u[: len(u) - len(suf)].rstrip("/")
            lower = u.lower()
    return u or "http://127.0.0.1:11434"


def is_ollama_mode() -> bool:
    """True when Mark should use local Ollama instead of Gemini."""
    env = os.environ.get("MARK_LLM_PROVIDER", "").strip().lower()
    if env == "ollama":
        return True
    if env == "gemini":
        return False
    data = _load_config()
    if (data.get("llm_provider") or "").strip().lower() == "ollama":
        return True
    return False


def get_ollama_url() -> str:
    raw = (
        os.environ.get("MARK_OLLAMA_URL", "").strip()
        or os.environ.get("ALETHEON_LLM_ASSIST_OLLAMA_URL", "").strip()
        or (_load_config().get("ollama_url") or "").strip()
        or "http://127.0.0.1:11434"
    )
    return _normalize_ollama_base(raw)


def get_ollama_model() -> str:
    return (
        os.environ.get("MARK_OLLAMA_MODEL", "").strip()
        or os.environ.get("ALETHEON_LLM_ASSIST_OLLAMA_MODEL", "").strip()
        or (_load_config().get("ollama_model") or "").strip()
        or "qwen2.5:7b"
    )


def get_ollama_request_options() -> dict:
    """
    Optional Ollama ``options`` merged into ``/api/chat`` (see Ollama modelfile docs).

    Set in ``config/api_keys.json`` as ``ollama_options``, e.g.::

        "ollama_options": {"num_ctx": 8192, "num_predict": 512, "temperature": 0.6}

    Lower ``num_predict`` caps reply length and can shorten time-to-first-token on
    small models; tune to your hardware.
    """
    data = _load_config()
    raw = data.get("ollama_options")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if isinstance(k, str) and k.strip():
            out[k.strip()] = v
    return out


def get_ollama_chat_timeout_sec() -> int:
    """
    HTTP read timeout (seconds) for ``POST /api/chat`` when the caller does not pass
    an explicit ``timeout`` (see ``ollama_chat``).

    Default **120** (was 600 — too long to wait on a stuck local Ollama).

    Override: env ``MARK_OLLAMA_CHAT_TIMEOUT`` or ``ollama_chat_timeout_sec`` in
    ``config/api_keys.json`` (integer seconds, clamped 30–600).
    """
    raw = os.environ.get("MARK_OLLAMA_CHAT_TIMEOUT", "").strip()
    if not raw:
        cfg = _load_config().get("ollama_chat_timeout_sec")
        if isinstance(cfg, (int, float)) and int(cfg) > 0:
            raw = str(int(cfg))
        elif isinstance(cfg, str) and cfg.strip().isdigit():
            raw = cfg.strip()
    if raw.isdigit():
        return max(30, min(int(raw), 600))
    return 120


def get_ollama_vision_model() -> str:
    """
    Vision-capable Ollama tag for screen/camera tools (separate from chat model).

    Set ``MARK_OLLAMA_VISION_MODEL`` or ``ollama_vision_model`` in ``api_keys.json``.
    Default ``llava`` — run ``ollama pull llava`` (or another vision tag) before use.
    """
    v = (
        os.environ.get("MARK_OLLAMA_VISION_MODEL", "").strip()
        or (_load_config().get("ollama_vision_model") or "").strip()
    )
    return v or "llava"


# Prebuilt Gemini TTS / Live voice names (see Gemini speech generation docs).
GEMINI_TTS_VOICE_NAMES: tuple[str, ...] = (
    "Achernar",
    "Achird",
    "Algenib",
    "Algieba",
    "Alnilam",
    "Aoede",
    "Autonoe",
    "Callirrhoe",
    "Charon",
    "Despina",
    "Enceladus",
    "Erinome",
    "Fenrir",
    "Gacrux",
    "Iapetus",
    "Kore",
    "Laomedeia",
    "Leda",
    "Orus",
    "Puck",
    "Pulcherrima",
    "Rasalgethi",
    "Sadachbia",
    "Sadaltager",
    "Schedar",
    "Sulafat",
    "Umbriel",
    "Vindemiatrix",
    "Zephyr",
    "Zubenelgenubi",
)


def get_gemini_live_voice_name() -> str:
    """
    Prebuilt Gemini voice id for **Live** sessions and for **local Gemini TTS**
    (when ``tts_backend`` is ``gemini``).

    Override with ``MARK_GEMINI_VOICE`` / ``GEMINI_LIVE_VOICE``, or
    ``gemini_live_voice`` / ``gemini_voice_name`` in ``config/api_keys.json``.
    Default ``Charon``. Unknown names fall back to **Kore** so TTS API requests stay valid.

    See Gemini speech docs for the full list (same ids as ``GEMINI_TTS_VOICE_NAMES``).
    """
    raw = (
        os.environ.get("MARK_GEMINI_VOICE", "").strip()
        or os.environ.get("GEMINI_LIVE_VOICE", "").strip()
        or (_load_config().get("gemini_live_voice") or "").strip()
        or (_load_config().get("gemini_voice_name") or "").strip()
    )
    v = (raw or "Charon").strip() or "Charon"
    by_lower = {n.lower(): n for n in GEMINI_TTS_VOICE_NAMES}
    if v.lower() in by_lower:
        return by_lower[v.lower()]
    print(
        f"[TTS] gemini_live_voice {v!r} is not a known prebuilt id — using 'Kore'. "
        "Pick a name from the VOICE OUTPUT (LOCAL) Gemini list in the UI."
    )
    return "Kore"


def get_local_tts_voice_substring() -> str | None:
    """
    Substring matched against Windows SAPI voice **display names** for ``pyttsx3``.

    First voice whose ``name`` contains this substring (case-insensitive) is used.
    Set ``MARK_TTS_VOICE``, or ``tts_voice_substring`` / ``local_tts_voice`` in
    ``config/api_keys.json``. Example substrings: ``David``, ``Zira``, ``Mark``.
    """
    v = (
        os.environ.get("MARK_TTS_VOICE", "").strip()
        or (_load_config().get("tts_voice_substring") or "").strip()
        or (_load_config().get("local_tts_voice") or "").strip()
    )
    return v or None


def list_gemini_tts_voice_names() -> list[str]:
    return list(GEMINI_TTS_VOICE_NAMES)


# Coqui **registry** ids for local inference (no Tortoise). UI presets only — you can
# still type any other registry id in the editable combo; see Coqui model zoo docs.
COQUI_TTS_MODEL_PRESETS: tuple[str, ...] = (
    "tts_models/en/ljspeech/tacotron2-DDC",
    "tts_models/en/ljspeech/glow-tts",
    "tts_models/en/ljspeech/speedy-speech-tts",
    "tts_models/en/vctk/vits",
    "tts_models/en/jenny/jenny",
    "tts_models/en/sam/tacotron-DDC",
    "tts_models/en/ek1/tacotron2",
)


def list_coqui_tts_model_presets() -> list[str]:
    """Preset ``coqui_model_name`` values shown when ``tts_backend`` is Coqui."""
    return list(COQUI_TTS_MODEL_PRESETS)


def get_gemini_api_key() -> str:
    """Same key as full Gemini mode: ``gemini_api_key`` in ``api_keys.json`` (or env)."""
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or (_load_config().get("gemini_api_key") or "").strip()
    )


def get_local_tts_backend() -> str:
    """
    Local reply speech: ``pyttsx3`` (Windows SAPI), ``gemini`` (Gemini TTS),
    or ``coqui`` (local TechGym / Coqui repo). Windows SAPI is always the fallback
    if the primary backend fails or is not configured.

    ``MARK_TTS_BACKEND`` or ``tts_backend``: ``pyttsx3`` | ``gemini`` | ``google`` |
    ``coqui`` | ``techgym``. Legacy ``voxcpm`` / ``voxcpm2`` values are remapped to
    ``coqui`` (VoxCPM was removed from Mark).
    """
    v = (
        os.environ.get("MARK_TTS_BACKEND", "").strip().lower()
        or (_load_config().get("tts_backend") or "").strip().lower()
    )
    if v in ("gemini", "google"):
        return "gemini"
    if v in ("coqui", "techgym", "voxcpm", "voxcpm2"):
        return "coqui"
    return "pyttsx3"


def get_coqui_tts_repo_path() -> str:
    """Root of your TechGym TTS clone (directory that contains ``TTS/``)."""
    return (
        os.environ.get("MARK_COQUI_REPO", "").strip()
        or (_load_config().get("coqui_tts_repo_path") or "").strip()
    )


def get_coqui_model_name() -> str:
    """
    Coqui **registry** model id for inference (not the LJSpeech training dataset).

    Example: ``tts_models/en/ljspeech/tacotron2-DDC`` — first load may download pretrained
    weights into Coqui's cache. Training recipes (``download_ljspeech.sh``, ``train_*.py``)
    are only needed if you are building your own checkpoint; then point ``coqui_model_path``
    / ``coqui_config_path`` at that run's outputs instead.
    """
    return (
        os.environ.get("MARK_COQUI_MODEL_NAME", "").strip()
        or (_load_config().get("coqui_model_name") or "").strip()
    )


def get_coqui_model_path() -> str:
    """Optional: offline checkpoint ``.pth`` (use with ``coqui_config_path``)."""
    return (
        os.environ.get("MARK_COQUI_MODEL_PATH", "").strip()
        or (_load_config().get("coqui_model_path") or "").strip()
    )


def get_coqui_config_path() -> str:
    """Optional: model ``config.json`` alongside ``coqui_model_path``."""
    return (
        os.environ.get("MARK_COQUI_CONFIG_PATH", "").strip()
        or (_load_config().get("coqui_config_path") or "").strip()
    )


def get_coqui_vocoder_path() -> str:
    return (
        os.environ.get("MARK_COQUI_VOCODER_PATH", "").strip()
        or (_load_config().get("coqui_vocoder_path") or "").strip()
    )


def get_coqui_vocoder_config_path() -> str:
    return (
        os.environ.get("MARK_COQUI_VOCODER_CONFIG_PATH", "").strip()
        or (_load_config().get("coqui_vocoder_config_path") or "").strip()
    )


def get_coqui_use_cuda() -> bool:
    """
    Whether Coqui should ask PyTorch for GPU. If config/env requests GPU but
    ``torch.cuda.is_available()`` is false (CPU-only torch, no driver, etc.),
    returns **False** so Coqui does not assert-fail at model load.
    """
    global _coqui_cuda_cpu_note_printed
    raw = os.environ.get("MARK_COQUI_CUDA", "").strip().lower()
    if raw in ("0", "false", "no", "off", "cpu"):
        return False
    if raw in ("1", "true", "yes", "on", "cuda", "gpu"):
        want_cuda = True
    else:
        data = _load_config()
        v = data.get("coqui_use_cuda")
        if isinstance(v, bool):
            want_cuda = v
        elif isinstance(v, str) and v.strip().lower() in ("0", "false", "no"):
            want_cuda = False
        else:
            want_cuda = True
    if not want_cuda:
        return False
    try:
        import torch

        if torch.cuda.is_available():
            return True
    except Exception:
        pass
    if not _coqui_cuda_cpu_note_printed:
        print(
            "[TTS] Coqui: GPU requested (``coqui_use_cuda`` / default) but CUDA is not "
            "available in this Python — using **CPU**. Set ``coqui_use_cuda``: false in "
            "api_keys.json to silence this, or install CUDA-enabled PyTorch + drivers."
        )
        _coqui_cuda_cpu_note_printed = True
    return False


def get_coqui_speaker() -> str:
    """Speaker id for multi-speaker models (optional)."""
    return (
        os.environ.get("MARK_COQUI_SPEAKER", "").strip()
        or (_load_config().get("coqui_speaker") or "").strip()
    )


def get_coqui_language() -> str:
    """Language code for multilingual models (optional)."""
    return (
        os.environ.get("MARK_COQUI_LANGUAGE", "").strip()
        or (_load_config().get("coqui_language") or "").strip()
    )


def coqui_engine_disk_signature(data: dict | None) -> str:
    """
    Stable fingerprint of **on-disk** Coqui settings from ``api_keys.json`` (or a dict
    about to be written). Used to avoid dropping the cached Coqui engine when the UI
    re-saves identical Coqui fields (e.g. ``editingFinished`` without real changes).
    """
    if not isinstance(data, dict):
        return ""
    tb = (data.get("tts_backend") or "").strip().lower()
    if tb not in ("coqui", "techgym"):
        return ""
    keys = (
        "coqui_tts_repo_path",
        "coqui_model_name",
        "coqui_model_path",
        "coqui_config_path",
        "coqui_vocoder_path",
        "coqui_vocoder_config_path",
        "coqui_use_cuda",
        "coqui_speaker",
        "coqui_language",
    )
    parts: list[str] = []
    for k in keys:
        v = data.get(k)
        if isinstance(v, bool):
            parts.append("1" if v else "0")
        elif v is None:
            parts.append("")
        else:
            parts.append(str(v).strip())
    return "\x1e".join(parts)


def get_coqui_failover_to_gemini() -> bool:
    """
    When ``tts_backend`` is ``coqui``, if True and ``gemini_api_key`` is set,
    try Gemini TTS after Coqui fails (before Windows SAPI). If False, Coqui
    failures go straight to SAPI; you can keep a Gemini key for chat or
    ``tts_backend: gemini`` without implying cloud speech on every Coqui miss.

    ``MARK_COQUI_FAILOVER_TO_GEMINI``: ``1``/``true``/``yes``/``on`` or
    ``0``/``false``/``no``/``off`` (when set, overrides file). File key:
    ``coqui_failover_to_gemini`` (boolean). Default **False** (local-first).
    """
    raw = os.environ.get("MARK_COQUI_FAILOVER_TO_GEMINI", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    data = _load_config()
    v = data.get("coqui_failover_to_gemini")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return False


def get_gemini_tts_model() -> str:
    """
    Gemini TTS model id for ``generate_content`` (see Google speech docs).

    Default ``gemini-2.5-flash-preview-tts`` — often shares quota with other **2.5 Flash**
    usage on your project. To prefer **3.1** instead, set ``gemini_tts_model`` in
    ``api_keys.json`` (or ``MARK_GEMINI_TTS_MODEL``) to ``gemini-3.1-flash-tts-preview``.
    ``mark_tts`` retries other known TTS models on 429 before falling back to SAPI.
    """
    v = (
        os.environ.get("MARK_GEMINI_TTS_MODEL", "").strip()
        or (_load_config().get("gemini_tts_model") or "").strip()
    )
    return v or "gemini-2.5-flash-preview-tts"


def get_weather_default_cities() -> list[str]:
    """
    Default place names for ``weather_report`` when the model omits or placeholders ``city``.

    Priority:

    1. ``weather_cities`` in ``api_keys.json`` — JSON array of strings, e.g.
       ``["Lehigh, FL", "Miami, FL"]``.
    2. ``MARK_WEATHER_CITY`` — semicolon-separated list, e.g. ``Lehigh, FL; Miami, FL``.
    3. ``weather_city`` or ``default_city`` in ``api_keys.json`` (single string).
    """
    data = _load_config()
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        t = s.strip()
        if not t or len(t) > 120:
            return
        k = t.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(t)

    raw_list = data.get("weather_cities")
    if isinstance(raw_list, list):
        for x in raw_list:
            if isinstance(x, str):
                _add(x)
        if out:
            return out

    env = os.environ.get("MARK_WEATHER_CITY", "").strip()
    if env:
        for part in env.split(";"):
            _add(part)
        if out:
            return out

    one = (data.get("weather_city") or "").strip() or (data.get("default_city") or "").strip()
    if one:
        _add(one)
    return out


def get_weather_default_city() -> str:
    """First default location, or empty string (backward compatible)."""
    cities = get_weather_default_cities()
    return cities[0] if cities else ""


def get_weather_open_browser() -> bool:
    """If True, also open a Google search tab after a successful forecast (default False)."""
    ev = os.environ.get("MARK_WEATHER_OPEN_BROWSER", "").strip().lower()
    if ev in ("1", "true", "yes", "on"):
        return True
    if ev in ("0", "false", "no", "off"):
        return False
    v = (_load_config().get("weather_open_browser") or "")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def get_weather_use_imperial_units() -> bool:
    """Fahrenheit + mph when True (default on Windows in config)."""
    ev = os.environ.get("MARK_WEATHER_IMPERIAL", "").strip().lower()
    if ev in ("1", "true", "yes", "imperial", "f"):
        return True
    if ev in ("0", "false", "no", "metric", "c"):
        return False
    u = (_load_config().get("weather_units") or "").strip().lower()
    if u in ("metric", "celsius", "c"):
        return False
    if u in ("imperial", "fahrenheit", "f"):
        return True
    osname = (_load_config().get("os_system") or "").strip().lower()
    return osname == "windows"


def ollama_model_env_locked() -> bool:
    """When True, ``MARK_OLLAMA_MODEL`` / Aletheon env overrides config and the UI picker."""
    return bool(
        os.environ.get("MARK_OLLAMA_MODEL", "").strip()
        or os.environ.get("ALETHEON_LLM_ASSIST_OLLAMA_MODEL", "").strip()
    )


def list_ollama_models() -> list[str]:
    """
    Return model tags reported by the local Ollama daemon (``GET /api/tags``),
    same idea as Aletheon's ``list_available_models`` for the assist UI.
    """
    import requests

    base = get_ollama_url().rstrip("/")
    resp = requests.get(f"{base}/api/tags", timeout=20)
    resp.raise_for_status()
    body = resp.json()
    raw = body.get("models")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for row in raw:
        if isinstance(row, dict):
            name = row.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return list(dict.fromkeys(names))


def ollama_chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    model: str | None = None,
    timeout: int | None = None,
) -> dict:
    """POST /api/chat (non-streaming). Returns parsed JSON or raises on HTTP error."""
    import requests

    effective_timeout = get_ollama_chat_timeout_sec() if timeout is None else timeout

    url = f"{get_ollama_url()}/api/chat"
    payload: dict = {
        "model": (model or "").strip() or get_ollama_model(),
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    opts = get_ollama_request_options()
    if opts:
        payload["options"] = opts
    resp = requests.post(url, json=payload, timeout=effective_timeout)
    if not resp.ok:
        body = (resp.text or "").strip()[:1200]
        print(f"[OLLAMA] HTTP {resp.status_code} {url}\n{body}")
        if resp.status_code == 405:
            print(
                "[OLLAMA] HTTP 405: /api/chat expects POST from the app, not GET in a browser. "
                "If this persists, check ollama_url is only the daemon root "
                "(e.g. http://127.0.0.1:11434) and that Ollama is running on that port."
            )
        if resp.status_code >= 500:
            print(
                "[OLLAMA] HTTP 5xx: check `ollama ps`, GPU VRAM, and `ollama logs`; "
                "try `ollama pull` again or a smaller model if OOM."
            )
    resp.raise_for_status()
    return resp.json()


def ollama_generate_text(
    user_prompt: str,
    *,
    system_instruction: str | None = None,
    timeout: int | None = None,
) -> str:
    """Single-turn text generation via /api/chat (no tools)."""
    messages: list[dict] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": user_prompt})
    data = ollama_chat(messages, tools=None, timeout=timeout)
    msg = data.get("message") or {}
    return (msg.get("content") or "").strip()


def ollama_chat_with_image_reply(
    user_text: str,
    image_b64: str,
    *,
    system: str | None = None,
    model: str | None = None,
    tools: list[dict] | None = None,
    timeout: int = 180,
) -> str:
    """One user turn with a base64-encoded image (PNG/JPEG) for a vision model."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": user_text,
            "images": [image_b64],
        }
    )
    data = ollama_chat(
        messages,
        tools=tools,
        model=model or get_ollama_vision_model(),
        timeout=timeout,
    )
    msg = data.get("message") or {}
    return (msg.get("content") or "").strip()
