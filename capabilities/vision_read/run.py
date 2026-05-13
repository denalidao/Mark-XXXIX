"""Vision Read capability: local-only Ollama screen/camera understanding."""

from __future__ import annotations

import base64
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

try:
    import cv2

    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools

    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image

    _PIL = True
except ImportError:
    _PIL = False


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CAP_ID = Path(__file__).resolve().parent.name


def _ensure_repo_path() -> None:
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _read_stdin_json() -> dict:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"_parse_error": str(e), "_raw": raw[:500]}


def _load_router_meta() -> dict:
    _ensure_repo_path()
    from playbook_runner import get_router_capability_meta

    return get_router_capability_meta(_CAP_ID)


def _load_config() -> dict:
    cfg_path = _REPO_ROOT / "config" / "api_keys.json"
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _emit(obj: dict, *, ok: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def _get_ollama_url(meta: dict) -> str:
    url = (
        str(meta.get("ollama_url") or "").strip()
        or str(os.environ.get("MARK_OLLAMA_URL", "")).strip()
        or str(_load_config().get("ollama_url") or "").strip()
        or "http://127.0.0.1:11434"
    )
    return url.rstrip("/")


def _get_vision_model(meta: dict) -> str:
    m = (
        str(meta.get("vision_model") or "").strip()
        or str(os.environ.get("MARK_OLLAMA_VISION_MODEL", "")).strip()
        or str(_load_config().get("ollama_vision_model") or "").strip()
        or "llava"
    )
    return m


def _http_json(url: str, method: str = "GET", body: dict | None = None, timeout: int = 15) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def _health(ollama_url: str, model: str, timeout: int) -> dict:
    started = time.time()
    try:
        tags = _http_json(f"{ollama_url}/api/tags", timeout=timeout)
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "error_code": "OLLAMA_UNREACHABLE",
            "detail": str(e),
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error_code": "OLLAMA_HEALTH_ERROR",
            "detail": str(e),
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    names: list[str] = []
    for item in (tags.get("models") or []):
        n = str(item.get("name") or "").strip()
        if n:
            names.append(n)
    has_model = any(n == model or n.startswith(f"{model}:") for n in names)
    return {
        "ok": has_model,
        "error_code": "" if has_model else "MODEL_NOT_FOUND",
        "detail": "" if has_model else f"Vision model {model!r} not found in ollama list.",
        "ollama_url": ollama_url,
        "vision_model": model,
        "installed_models": names[:30],
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _capture_screen() -> tuple[bool, bytes | None, str]:
    if not _MSS:
        return False, None, "mss not installed."
    with mss.mss() as sct:
        mons = sct.monitors
        mon = mons[1] if len(mons) > 1 else mons[0]
        shot = sct.grab(mon)
        png = mss.tools.to_png(shot.rgb, shot.size)
    return True, png, ""


def _get_os() -> str:
    return str(_load_config().get("os_system") or platform.system().lower())


def _capture_camera() -> tuple[bool, bytes | None, str]:
    if not _CV2:
        return False, None, "opencv-python not installed."
    os_name = _get_os().lower()
    backend = cv2.CAP_DSHOW if "win" in os_name else cv2.CAP_ANY
    cap = cv2.VideoCapture(0, backend)
    if not cap.isOpened():
        return False, None, "camera index 0 could not be opened."
    for _ in range(8):
        cap.read()
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return False, None, "camera returned no frame."
    ok_enc, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok_enc:
        return False, None, "camera encode failed."
    return True, buf.tobytes(), ""


def _ask_ollama(ollama_url: str, model: str, question: str, image_bytes: bytes, timeout: int) -> tuple[bool, str, str]:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise visual assistant. Answer only from the provided image. "
                    "If unreadable, say so clearly."
                ),
            },
            {"role": "user", "content": question, "images": [b64]},
        ],
    }
    try:
        out = _http_json(f"{ollama_url}/api/chat", method="POST", body=payload, timeout=timeout)
        msg = out.get("message") or {}
        text = str(msg.get("content") or "").strip()
        if not text:
            return False, "", "Empty response from Ollama."
        return True, text, ""
    except urllib.error.URLError as e:
        return False, "", f"OLLAMA_UNREACHABLE: {e}"
    except TimeoutError:
        return False, "", "OLLAMA_TIMEOUT"
    except Exception as e:
        return False, "", str(e)


def main() -> None:
    args = _read_stdin_json()
    if "_parse_error" in args:
        _emit({"ok": False, "capability": _CAP_ID, "error": "stdin JSON parse failed", "detail": args["_parse_error"]}, ok=False)

    try:
        meta = _load_router_meta()
    except Exception as e:
        _emit({"ok": False, "capability": _CAP_ID, "error": str(e)}, ok=False)

    action = str(args.get("action") or "health").strip().lower()
    question = str(args.get("question") or args.get("text") or "").strip()
    timeout = int(meta.get("request_timeout_sec", 90))
    health_timeout = int(meta.get("health_timeout_sec", 10))

    ollama_url = _get_ollama_url(meta)
    model = _get_vision_model(meta)

    if action == "health":
        status = _health(ollama_url, model, health_timeout)
        _emit({"capability": _CAP_ID, "action": "health", **status}, ok=bool(status.get("ok")))

    if action not in ("read_screen", "read_camera"):
        _emit(
            {
                "ok": False,
                "capability": _CAP_ID,
                "error": f"unknown action {action!r}; use health, read_screen, or read_camera",
            },
            ok=False,
        )

    if not question:
        _emit({"ok": False, "capability": _CAP_ID, "action": action, "error": "question/text is required."}, ok=False)

    status = _health(ollama_url, model, health_timeout)
    if not status.get("ok"):
        _emit({"capability": _CAP_ID, "action": action, **status}, ok=False)

    if action == "read_screen":
        ok_cap, image, cap_err = _capture_screen()
        src = "screen"
    else:
        ok_cap, image, cap_err = _capture_camera()
        src = "camera"
    if not ok_cap or image is None:
        _emit(
            {
                "ok": False,
                "capability": _CAP_ID,
                "action": action,
                "source": src,
                "error_code": "CAPTURE_FAILED",
                "detail": cap_err,
            },
            ok=False,
        )

    ok_ans, answer, err = _ask_ollama(ollama_url, model, question, image, timeout=timeout)
    if not ok_ans:
        code = "OLLAMA_TIMEOUT" if "TIMEOUT" in err.upper() else "OLLAMA_VISION_FAILED"
        _emit(
            {
                "ok": False,
                "capability": _CAP_ID,
                "action": action,
                "source": src,
                "error_code": code,
                "detail": err,
                "ollama_url": ollama_url,
                "vision_model": model,
            },
            ok=False,
        )

    _emit(
        {
            "ok": True,
            "capability": _CAP_ID,
            "action": action,
            "source": src,
            "answer": answer,
            "ollama_url": ollama_url,
            "vision_model": model,
        },
        ok=True,
    )


if __name__ == "__main__":
    main()
