"""
Rolling session transcript on disk (JSON). Long chats stay out of the Ollama
message list except as a **bounded** excerpt merged into the system prompt.

``long_term.json`` remains the store for durable facts; this file is **session
continuity** (recent user/assistant text only), not tool payloads.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
SESSION_PATH = BASE_DIR / "memory" / "session_context.json"
_lock = RLock()

MAX_TURNS_STORED = 120
MAX_TURNS_IN_PROMPT = 14
MAX_USER_STORE = 4000
MAX_ASSISTANT_STORE = 8000
MAX_USER_PROMPT = 900
MAX_ASSISTANT_PROMPT = 1400
MAX_SESSION_PROMPT_CHARS = 3200


def _empty_doc() -> dict:
    return {"version": 1, "turns": []}


def load_session_doc() -> dict:
    if not SESSION_PATH.exists():
        return _empty_doc()
    with _lock:
        try:
            raw = SESSION_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return _empty_doc()
            turns = data.get("turns")
            if not isinstance(turns, list):
                turns = []
            return {"version": int(data.get("version", 1)), "turns": turns}
        except Exception as e:
            print(f"[Session] ⚠️ load error: {e}")
            return _empty_doc()


def _trim_stored_turns(turns: list) -> list:
    if len(turns) <= MAX_TURNS_STORED:
        return turns
    drop = len(turns) - MAX_TURNS_STORED
    print(f"[Session] 🗑️ dropped {drop} oldest turn(s) (cap {MAX_TURNS_STORED})")
    return turns[drop:]


def save_session_doc(doc: dict) -> None:
    if not isinstance(doc, dict):
        return
    turns = doc.get("turns")
    if not isinstance(turns, list):
        turns = []
    doc = {"version": int(doc.get("version", 1)), "turns": _trim_stored_turns(turns)}
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        SESSION_PATH.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def clear_session_context() -> None:
    """Wipe the on-disk session transcript (UI or tests can call this)."""
    save_session_doc(_empty_doc())
    print("[Session] Cleared session_context.json")


def maybe_clear_session_via_env() -> None:
    """
    If ``MARK_CLEAR_SESSION_CONTEXT`` is truthy, clear the JSON file once and
    unset the variable so the next turn behaves normally.
    """
    v = os.environ.get("MARK_CLEAR_SESSION_CONTEXT", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        os.environ.pop("MARK_CLEAR_SESSION_CONTEXT", None)
        clear_session_context()


def append_session_turn(user: str, assistant: str) -> None:
    """Append one completed exchange after the model has answered."""
    u = (user or "").strip()
    a = (assistant or "").strip()
    if not u and not a:
        return
    if len(u) > MAX_USER_STORE:
        u = u[: MAX_USER_STORE - 1].rstrip() + "…"
    if len(a) > MAX_ASSISTANT_STORE:
        a = a[: MAX_ASSISTANT_STORE - 1].rstrip() + "…"
    entry = {
        "user": u,
        "assistant": a,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with _lock:
        doc = load_session_doc()
        turns = doc.get("turns")
        if not isinstance(turns, list):
            turns = []
        turns.append(entry)
        doc["turns"] = _trim_stored_turns(turns)
        save_session_doc(doc)


def _clip(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _merge_same_user_turns(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep latest assistant reply when the user repeated the same line."""
    out: list[tuple[str, str]] = []
    for u, a in pairs:
        u = u.strip()
        if out and out[-1][0] == u:
            out[-1] = (u, a)
        else:
            out.append((u, a))
    return out


def format_session_context_for_prompt() -> str:
    """
    Compact excerpt for the system prompt — not full chat history or tool JSON.
    """
    doc = load_session_doc()
    turns = doc.get("turns")
    if not isinstance(turns, list) or not turns:
        return ""
    window = turns[-MAX_TURNS_IN_PROMPT:]
    pairs: list[tuple[str, str]] = []
    for t in window:
        if not isinstance(t, dict):
            continue
        u = str(t.get("user") or "")
        a = str(t.get("assistant") or "")
        pairs.append((u, a))
    pairs = _merge_same_user_turns(pairs)
    blocks: list[str] = []
    for u, a in pairs:
        blocks.append(
            "User: "
            + _clip(u, MAX_USER_PROMPT)
            + "\nAssistant: "
            + _clip(a, MAX_ASSISTANT_PROMPT)
        )
    body = "\n\n".join(blocks)
    if len(body) > MAX_SESSION_PROMPT_CHARS:
        body = "…\n" + body[-(MAX_SESSION_PROMPT_CHARS - 2) :]
    return (
        "\n[RECENT CONVERSATION — from session_context.json; bounded excerpt, "
        "not tool logs]\n"
        "Use for **continuity** only; facts live in **long-term memory** and "
        "**tools**.\n\n"
        + body
        + "\n"
    )
