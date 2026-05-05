"""Local Ollama backend: text commands + tool calling + offline TTS (no Gemini API)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Union

import mark_voice
import requests
from mark_llm_settings import get_ollama_model, get_ollama_url, ollama_chat
from mark_tts import speak_mark_tts

from jarvis_tool_runner import (
    ollama_tools_from_gemini_declarations,
    parse_tool_arguments,
    refine_send_message_args,
    refine_web_search_args,
    run_jarvis_tool,
    synthetic_tool_calls_from_text,
    user_text_explicitly_requests_real_message,
)
from memory.memory_manager import (
    assistant_persona_final_override,
    format_memory_for_prompt,
    load_memory,
)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


PROMPT_PATH = _base_dir() / "core" / "prompt.txt"


def _tts_say(
    text: str,
    *,
    on_audio_start: Callable[[], None] | None = None,
) -> None:
    if os.environ.get("MARK_DISABLE_TTS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    if not (text or "").strip():
        return
    try:
        speak_mark_tts(text, on_audio_start=on_audio_start)
    except Exception as e:
        print(f"[TTS] speak failed: {e}")
        raise


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )


QueueItem = Union[str, tuple[str, bytes, int]]


def _ollama_tool_message_body(tool_name: str, payload: dict[str, Any]) -> str:
    """
    Ollama small models often ignore ``{\"result\": ...}`` JSON. Use explicit prose
    so the model treats tool output as real retrieved data to summarize.
    """
    raw = payload.get("result")
    if isinstance(raw, (dict, list)):
        raw_s = json.dumps(raw, ensure_ascii=False)
    elif raw is None:
        raw_s = ""
    else:
        raw_s = str(raw)
    max_len = 12_000
    if len(raw_s) > max_len:
        raw_s = raw_s[: max_len - 3] + "..."
    if tool_name == "web_search":
        return (
            "WEB SEARCH SNIPPETS (just fetched by the host; these are real page titles "
            "and excerpts, not your prior knowledge). If you see numbered headlines below, "
            "that **is** the retrieved material — summarize it for the user. Do **not** "
            "apologize for being unable to fetch news, say you lack real-time access, or ask "
            "them to open a browser, unless this block is empty or explicitly says no results.\n\n"
            "---\n\n"
            + raw_s
        )
    if tool_name == "weather_report":
        return (
            "WEATHER TOOL OUTPUT (already fetched). Give a brief spoken summary; do not ask "
            "to look up weather again unless this block says there was an error.\n\n---\n\n"
            + raw_s
        )
    if tool_name == "send_message" and isinstance(raw_s, str) and raw_s.startswith("SKIPPED"):
        return (
            "SEND_MESSAGE (host did **not** open messaging apps):\n\n"
            + raw_s
        )
    return json.dumps(payload, ensure_ascii=False)


def _ollama_tool_names(tools: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        n = fn.get("name") or ""
        if isinstance(n, str) and n.strip():
            names.add(n.strip())
    return names


def _messages_tail_since_last_user(messages: list[dict]) -> list[dict]:
    """Slice of ``messages`` after the most recent ``role: user`` (current turn)."""
    idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            idx = i
            break
    if idx is None:
        return list(messages)
    return messages[idx + 1 :]


def _has_open_app_compose_instruct_in_tail(tail: list[dict]) -> bool:
    """True if ``open_app`` already returned the compose follow-up marker this turn."""
    for m in tail:
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if isinstance(c, str) and "HOST_INSTRUCT" in c:
            return True
    return False


def _strip_markdown_fences(s: str) -> str:
    t = (s or "").strip()
    for pat in (
        r"^`{3}[a-zA-Z0-9_-]*\s*\r?\n([\s\S]*?)\r?\n`{3}\s*$",
        r"^`{3}\s*\r?\n([\s\S]*?)\r?\n`{3}\s*$",
        r"^`{3}[a-zA-Z0-9_-]*\s*\r?\n([\s\S]*?)`{3}\s*$",
        r"^`{3}\s*\r?\n([\s\S]*?)`{3}\s*$",
    ):
        m = re.match(pat, t)
        if m:
            return m.group(1).strip()
    return t


def _prose_suitable_for_notepad_compose(content: str) -> bool:
    """Heuristic: assistant returned body text meant for the editor, not a short chat reply."""
    t = _strip_markdown_fences(content).strip()
    if len(t) < 60:
        return False
    if t.lstrip().startswith("{"):
        return False
    if "def " in t or "\nimport " in t or t.lower().startswith("#include"):
        return False
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) < 2 and len(t) < 200:
        return False
    return True


def _split_for_progressive_speech(text: str, *, max_chunk_chars: int = 260) -> list[str]:
    """
    Split long replies into sentence-like chunks so TTS starts earlier.
    """
    t = (text or "").strip()
    if not t:
        return []
    parts = re.findall(r"[^.!?\n]+[.!?]?|\S+", t)
    out: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        cand = f"{cur} {p}".strip() if cur else p
        if cur and len(cand) > max_chunk_chars:
            out.append(cur.strip())
            cur = p
        else:
            cur = cand
    if cur.strip():
        out.append(cur.strip())
    return out


def _user_means_read_browser_page(user_text: str) -> bool:
    """
    Short follow-ups like \"read?\" / \"read the page\" after ``go_to`` should use
    ``browser_control(get_text)``, not ``screen_process`` / vision.
    """
    t = (user_text or "").strip()
    if not t or len(t) > 160:
        return False
    tl = t.lower()
    if re.search(r"(?i)\bwhat'?s?\s+on\s+(?:the\s+)?(?:screen|monitor)\b", tl):
        return False
    vision_cues = (
        "camera",
        "webcam",
        "screenshot",
        "vision mode",
        "what do you see",
        "what can you see",
        "looking at my",
        "on my monitor",
        "on the monitor",
        "this image",
        "this picture",
    )
    if any(c in tl for c in vision_cues):
        return False
    if re.search(r"(?i)\bread\s+(?:aloud|out\s+loud)\b", tl):
        return False
    if re.search(
        r"(?i)\bread\s+(?:the\s+)?(?:page|tab|site|browser|window)\b", tl
    ):
        return True
    if re.search(r"(?i)\bcan\s+you\s+read\b", tl) and not re.search(
        r"(?i)\b(book|pdf|file|document|email|minds?|thoughts?)\b", tl
    ):
        return True
    if re.fullmatch(r"(?i)read\s*\??", t):
        return True
    if re.fullmatch(r"(?i)please\s+read\s*\??", t):
        return True
    if re.search(r"(?i)\bread\s+(?:it|that)\b", tl):
        if "out loud" in tl or "aloud" in tl:
            return False
        return True
    return False


def _coerce_screen_process_to_browser_read(
    user_text: str,
    tool_calls: list[dict],
    *,
    valid_names: set[str],
) -> list[dict]:
    if not tool_calls:
        return tool_calls
    if "browser_control" not in valid_names or "screen_process" not in valid_names:
        return tool_calls
    if not _user_means_read_browser_page(user_text):
        return tool_calls
    out: list[dict] = []
    changed = False
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if name == "screen_process":
            changed = True
            row = dict(tc)
            row["function"] = {
                "name": "browser_control",
                "arguments": {"action": "get_text"},
            }
            out.append(row)
        else:
            out.append(tc)
    if changed:
        print(
            "[JARVIS] read-page intent: using browser_control(get_text) "
            "instead of screen_process."
        )
    return out


def _user_means_physical_camera_vision(user_text: str) -> bool:
    """
    Questions about objects **in front of the user**, in their **hands**, or on the
    desk from the webcam — need ``screen_process`` with ``angle: camera``, not the
    monitor capture and not asking the user to \"describe the screen\".
    """
    t = (user_text or "").strip()
    if not t or len(t) > 220:
        return False
    tl = t.lower()
    if re.search(r"(?i)\bwhat'?s?\s+on\s+(my\s+)?(screen|monitor|display)\b", tl):
        return False
    if re.search(r"(?i)\b(this|the)\s+tab\b", tl) and not re.search(
        r"(?i)\b(hand|hold|holding|desk|front\s+of)\b", tl
    ):
        return False
    if re.search(r"(?i)\bwhat\s+do\s+i\s+have\s+in\s+front\s+of\s+me\b", tl):
        return True
    if re.search(r"(?i)\bwhat\s+'s\s+in\s+front\s+of\s+me\b", tl):
        return True
    if re.search(r"(?i)\bwhat\s+have\s+i\s+got\s+in\s+front\s+of\s+me\b", tl):
        return True
    if re.search(r"(?i)\bwhat\s+am\s+i\s+holding\b", tl):
        return True
    if re.search(r"(?i)\bwhat\s+(is|'s)\s+in\s+my\s+hand\b", tl):
        return True
    if re.search(r"(?i)\bwhat\s+do\s+i\s+have\s+in\s+my\s+hand\b", tl):
        return True
    if re.search(r"(?i)\b(in\s+my\s+hand|in\s+this\s+hand)\b", tl) and re.search(
        r"(?i)\b(what|see|tell|look|identify)\b", tl
    ):
        return True
    if re.search(r"(?i)\bin\s+front\s+of\s+me\b", tl) and re.search(
        r"(?i)\bon\s+(my\s+)?screen\b", tl
    ):
        return False
    if re.search(r"(?i)\bin\s+front\s+of\s+me\b", tl) and re.search(
        r"(?i)\b(what|see|tell|identify|recognize|have\s+i\s+got)\b", tl
    ):
        return True
    if re.search(r"(?i)\b(can\s+you\s+)?see\s+what\s+i\s+('m\s+)?holding\b", tl):
        return True
    if re.search(r"(?i)\blook\s+at\s+what\s+i\s+('m\s+)?holding\b", tl):
        return True
    return False


def _ensure_camera_angle_for_hands_questions(
    user_text: str, tool_calls: list[dict]
) -> list[dict]:
    """If the model used ``screen_process`` but defaulted ``angle`` to screen, switch to camera."""
    if not tool_calls or not _user_means_physical_camera_vision(user_text):
        return tool_calls
    out: list[dict] = []
    changed = False
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if name != "screen_process":
            out.append(tc)
            continue
        args = parse_tool_arguments(fn.get("arguments"))
        if not isinstance(args, dict):
            args = {}
        ang = str(args.get("angle", "screen") or "screen").lower().strip()
        if ang == "camera":
            out.append(tc)
            continue
        q = (args.get("text") or "").strip() or user_text
        changed = True
        row = dict(tc)
        row["function"] = {
            "name": "screen_process",
            "arguments": {"angle": "camera", "text": q},
        }
        out.append(row)
    if changed:
        print("[JARVIS] Physical-scene intent: screen_process angle=camera.")
    return out


class JarvisOllama:
    """
    Ollama-powered assistant loop (Aletheon-style HTTP to ``/api/chat``).

    Use the text field, file uploads, or **hold-to-talk** (PTT) in the UI; PTT audio
    is transcribed with faster-whisper then sent to Ollama. Spoken replies use
    **Gemini TTS** (if ``tts_backend`` is ``gemini`` and ``gemini_api_key`` is set)
    or **pyttsx3** / SAPI otherwise.
    """

    def __init__(self, ui, tool_declarations: list[dict]) -> None:
        self.ui = ui
        self._tool_declarations = tool_declarations
        self._loop: asyncio.AbstractEventLoop | None = None
        self._user_queue: asyncio.Queue[QueueItem] | None = None
        self._ollama_tools = ollama_tools_from_gemini_declarations(tool_declarations)
        # Set False after Ollama returns HTTP 400 with tools (e.g. vision-only chat tag).
        self._ollama_tools_enabled = True
        self._coqui_preload_started = False

    def _build_system_instruction(self) -> str:
        from datetime import datetime

        memory = load_memory()
        mem_str = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()
        now = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )
        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)
        parts.append(
            "\n[LOCAL MODE]\n"
            "You are running on a local Ollama model with **no built-in web access**. "
            "You only know facts from this session, memory, and **tool results**. Never "
            "invent headlines, prices, sports scores, or \"breaking\" news — call "
            "**web_search** once with a query that matches the user's topic (e.g. "
            "\"top sports headlines today US\" for sports news, not a generic unrelated query) "
            "and then summarize what the tool returned.\n"
            "**News / current events:** If they ask what's in the news, headlines, "
            "\"what's going on today\", or similar **without** naming a narrow topic, still "
            "call **web_search** once with a sensible broad query (e.g. "
            "\"top world news today\" or \"US news headlines today\"). Do **not** refuse or "
            "ask them to pick a country first unless they already gave a clear geographic scope.\n"
            "**Opening / reading a website:** If they ask to **open** a URL in a visible "
            "browser window or drive the UI, use **browser_control**: ``action: go_to`` with "
            "a full **https://…** ``url``, then ``get_text`` when they want visible tab text. "
            "If they only need the **text of a page** without driving the browser, use "
            "**web_search** with ``mode: \"fetch\"`` and ``url`` (plain-text excerpt). Use "
            "**web_search** ``mode: \"news\"`` + ``query`` for headline-only news. Use "
            "**web_search** ``mode: \"search\"`` (default) for general snippets.\n"
            "**Reminders / cron (Windows):** **reminder** uses **Task Scheduler** + **toast "
            "notifications** — **not** the **Clock** app's alarm list. For **tomorrow** / "
            "**day after tomorrow**, compute **date** as the real calendar day (not **today**); "
            "the host will fix obvious **today** vs **tomorrow** mismatches. For \"alarm tomorrow "
            "at 9\", use ``recurrence: once`` unless they clearly asked for **every day** / "
            "**weekdays** / **weekly**. ``action: list`` lists JARVIS* tasks; ``action: "
            "cancel`` + ``task_name`` or ``job_name`` removes one. For **recurring** alerts, "
            "set ``recurrence`` to **daily**, **weekly**, or **weekdays**, pass ``date``/"
            "``time`` for the first anchor, a stable ``job_name``, and optional ``open_app_name`` "
            "(same tokens as **open_app**, e.g. **notepad**) after each notification.\n"
            "**Notepad / editors + writing:** If they ask to open Notepad (or Word, etc.) "
            "**and** write / compose / draft a poem, letter, note, etc., you must supply the "
            "**full text** yourself: call **computer_control** ``action: \"smart_type\"`` with "
            "``text`` set to the entire composition (after **open_app**), **or** "
            "**file_controller** ``create_file`` / ``write`` with the content. **Never** tell "
            "the user to type it for you.\n"
            "**Read page vs vision:** Short phrases like **read the page**, **read the tab**, "
            "**read it**, or **can you read (the page)?** mean **browser_control** with "
            "``action: get_text`` on the current tab — **not** **screen_process** / vision. "
            "Use **screen_process** with ``angle: \"screen\"`` when they mean the **monitor** "
            "(what's on my screen, this window, browser tab as pixels).\n"
            "**Webcam / hands / desk:** If they ask what they **hold**, what is **in front of "
            "them**, **in my hand**, **on my desk** (physical object), or **what do I have "
            "here** in a face-to-camera sense, call **screen_process** with ``angle: "
            "\"camera\"`` and put their exact question in ``text``. Do **not** ask them to "
            "describe the monitor — you must call the tool; the vision model sees the webcam.\n"
            "Never refuse \"read the page\" for policy reasons when **browser_control** exists; "
            "call ``get_text`` and summarize the tool output.\n"
            "Never print fake tool lines like ``web_search(query=...)``, "
            "``[web_search(query=...)]``, or ``weather_report(city: ...)`` as your final answer — "
            "either emit native tool_calls or JSON the host can parse; after tools run, reply in "
            "plain language. Do not add bracket wrappers or \"Summarize the results here\" "
            "placeholders instead of calling the tool.\n"
            "If the assistant message already includes **web_search** tool results in this "
            "turn's history, summarize them; do not say you did not search or that nothing "
            "was found when the tool output is non-empty.\n"
            "**Weather:** If the user asks about weather, temperature, forecast, rain, "
            "\"is it raining\", or \"today's weather\", you **must** call **weather_report** "
            "immediately once before answering — **never** ask for permission, access, or "
            "confirmation to use weather tools. If they named a place, pass it as ``city`` "
            "(STT may garble \"Lehigh Acres\" as \"Lea Acres\"; pass the phrase you heard; "
            "the host normalizes common typos). If they did not name a place, pass **no** "
            "``city`` field or ``city: \"\"`` — the app has default locations in config; "
            "**do not** ask for city, region, or zip unless the tool errors with no defaults. "
            "Never reply that you lack access or cannot check; the tool fetches live data. "
            "If the tool returns a forecast, summarize it (including rain vs dry from the report) "
            "— do not ask for location again.\n"
            "Use tools whenever the user asks for an action. After tools complete, reply "
            "briefly in natural language (the user hears your reply as voice — do not repeat "
            "every number already present verbatim in the tool result; one short summary is enough).\n"
            "If the user asks you to greet someone by name (e.g. \"say hi to my grandson "
            "Cayden\"), speak that greeting **in character** through your voice only. "
            "Do **not** call **send_message** unless they explicitly ask to **send**, "
            "**text**, **email**, **DM**, or **message on [named app]** — short social "
            "greetings are **not** desktop messaging tasks.\n"
            "**send_message:** Always pass **platform** as the desktop app they named "
            "(e.g. Proton Mail, Gmail, WhatsApp). Never omit **platform** and never default "
            "to WhatsApp when they asked for email or another client."
        )
        tail = assistant_persona_final_override(memory)
        if tail:
            parts.append(tail)
        return "\n".join(parts)

    def speak(self, text: str) -> None:
        if self._loop and text:
            asyncio.run_coroutine_threadsafe(
                self._speak_async(text),
                self._loop,
            )

    def speak_error(self, tool_name: str, error: str) -> None:
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    async def _speak_async(self, text: str) -> None:
        try:
            await asyncio.to_thread(
                _tts_say,
                text,
                on_audio_start=lambda: self.ui.set_state("SPEAKING"),
            )
        except Exception as ex:
            short = str(ex).strip()[:220]
            self.ui.write_log(f"SYS: Speech synthesis failed — {short}")
            traceback.print_exc()
        finally:
            if not self.ui.muted:
                self.ui.set_state("LISTENING")

    async def _speak_progressive(self, text: str) -> None:
        """
        Speak in smaller chunks so audio starts earlier for long replies.
        """
        chunks = _split_for_progressive_speech(text)
        if not chunks:
            return
        if len(chunks) == 1:
            await self._speak_async(chunks[0])
            return
        for chunk in chunks:
            await self._speak_async(chunk)

    def _wire_text_input(self) -> None:
        assert self._loop and self._user_queue

        def _enqueue(text: str) -> None:
            if not self._loop or not self._user_queue:
                return
            # Thread-safe: schedule asyncio.Queue.put (not put_nowait via call_soon_threadsafe).
            fut = asyncio.run_coroutine_threadsafe(
                self._user_queue.put(text),
                self._loop,
            )

            def _log_put_err(f) -> None:
                try:
                    exc = f.exception()
                except Exception:
                    return
                if exc is not None:
                    print(f"[JARVIS] ⚠️ queue put failed: {exc}")

            fut.add_done_callback(_log_put_err)

        self.ui.on_text_command = _enqueue

    def feed_pcm(self, pcm: bytes, sample_rate: int = 16000) -> None:
        """Queue raw int16 mono PCM from the UI PTT (thread-safe)."""
        if not self._loop or not self._user_queue:
            return
        fut = asyncio.run_coroutine_threadsafe(
            self._user_queue.put(("pcm", pcm, sample_rate)),
            self._loop,
        )

        def _log_err(f) -> None:
            try:
                exc = f.exception()
            except Exception:
                return
            if exc is not None:
                print(f"[JARVIS] ⚠️ PTT queue put failed: {exc}")

        fut.add_done_callback(_log_err)

    async def _ollama_chat_round(self, messages: list[dict]) -> dict:
        """
        POST /api/chat.

        On HTTP 400 or 500 while tools are enabled, retry once without tools and
        disable tools for the rest of the session (registry may reject tools, or
        the runner may crash on the tool payload / VRAM).
        """
        tools = self._ollama_tools if self._ollama_tools_enabled else None

        def _call(ts: list[dict] | None) -> dict:
            return ollama_chat(messages, tools=ts)

        try:
            return await asyncio.to_thread(_call, tools)
        except requests.HTTPError as e:
            resp = e.response
            hint = (resp.text or "").strip()[:400] if resp is not None else ""
            code = resp.status_code if resp is not None else None
            if code in (400, 500) and tools:
                if code == 400:
                    self.ui.write_log(
                        "SYS: Ollama HTTP 400 with tools — retrying without tools. "
                        "Pick a model that supports tools (e.g. qwen2.5:7b, llama3.2:3b) in OLLAMA MODEL."
                        + (f" Detail: {hint}" if hint else "")
                    )
                else:
                    self.ui.write_log(
                        "SYS: Ollama HTTP 500 with tools — retrying without tools "
                        "(often VRAM or runner error with this model + tool list)."
                        + (f" Detail: {hint}" if hint else "")
                    )
                self._ollama_tools_enabled = False
                return await asyncio.to_thread(_call, None)
            if hint:
                self.ui.write_log(f"ERR: Ollama HTTP {code}: {hint}")
            raise

    async def _run_one_exchange(self, user_text: str) -> None:
        assert self._user_queue is not None
        self.ui.set_state("THINKING")
        sys_instr = self._build_system_instruction()
        messages: list[dict] = [
            {"role": "system", "content": sys_instr},
            {"role": "user", "content": user_text},
        ]

        for _round in range(12):
            data = await self._ollama_chat_round(messages)
            msg = data.get("message") or {}
            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            valid_names = _ollama_tool_names(self._ollama_tools)
            if not tool_calls and content:
                synthetic = synthetic_tool_calls_from_text(
                    content, valid_names=valid_names
                )
                if synthetic:
                    self.ui.write_log(
                        "SYS: Model returned tool syntax as plain text — executing it once. "
                        "Prefer models that emit native tool_calls for reliability."
                    )
                    tool_calls = synthetic
                    content = ""

            if (
                not tool_calls
                and content
                and _has_open_app_compose_instruct_in_tail(
                    _messages_tail_since_last_user(messages)
                )
                and _prose_suitable_for_notepad_compose(content)
            ):
                loop = asyncio.get_running_loop()
                body = _strip_markdown_fences(content).strip()
                try:
                    await asyncio.sleep(0.35)
                    out = await run_jarvis_tool(
                        "computer_control",
                        {"action": "smart_type", "text": body},
                        ui=self.ui,
                        speak=self.speak,
                        speak_error=self.speak_error,
                        loop=loop,
                        speak_from_tools=False,
                        user_query=user_text,
                    )
                    raw = str((out or {}).get("result") or "")
                    ok = bool(
                        re.search(r"(?i)(smart-?typed|typed|pasted|clipboard)", raw)
                    )
                    content = (
                        "I've pasted that into Notepad for you, sir."
                        if ok
                        else (
                            "Notepad should be open, but pasting into the window may have failed "
                            "(click inside Notepad so it is focused, then ask me to try again)."
                        )
                    )
                    self.ui.write_log(
                        "SYS: Host ran computer_control(smart_type) from assistant prose "
                        "(compose-in-notepad recovery)."
                    )
                except Exception as ex:
                    print(f"[JARVIS] compose smart_type inject: {ex}")
                    content = f"I could not type into Notepad: {ex}"

            if (
                len(messages) == 2
                and not tool_calls
                and _user_means_physical_camera_vision(user_text)
                and "screen_process" in valid_names
            ):
                tool_calls = [
                    {
                        "function": {
                            "name": "screen_process",
                            "arguments": {
                                "angle": "camera",
                                "text": user_text,
                            },
                        }
                    }
                ]
                content = ""
                self.ui.write_log(
                    "SYS: Webcam / physical scene — invoking screen_process (camera)."
                )

            if (
                len(messages) == 2
                and not tool_calls
                and _user_means_read_browser_page(user_text)
                and not _user_means_physical_camera_vision(user_text)
                and "browser_control" in valid_names
            ):
                tool_calls = [
                    {
                        "function": {
                            "name": "browser_control",
                            "arguments": {"action": "get_text"},
                        }
                    }
                ]
                content = ""
                self.ui.write_log(
                    "SYS: Read-page intent — invoking browser_control(get_text)."
                )

            if tool_calls:
                tool_calls = _coerce_screen_process_to_browser_read(
                    user_text, tool_calls, valid_names=valid_names
                )
                tool_calls = _ensure_camera_angle_for_hands_questions(
                    user_text, tool_calls
                )
                if not user_text_explicitly_requests_real_message(user_text):
                    before = len(tool_calls)
                    tool_calls = [
                        tc
                        for tc in tool_calls
                        if ((tc.get("function") or {}).get("name") or "").strip()
                        != "send_message"
                    ]
                    if len(tool_calls) != before:
                        self.ui.write_log(
                            "SYS: Ignored send_message tool call — explicit outbound "
                            "messaging intent not found in user request."
                        )

            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if tool_calls:
                loop = asyncio.get_running_loop()
                tool_names_round = [
                    ((tc.get("function") or {}).get("name") or "").strip()
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]
                only_vision = bool(tool_names_round) and all(
                    n == "screen_process" for n in tool_names_round
                )
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tname = fn.get("name") or ""
                    args = parse_tool_arguments(fn.get("arguments"))
                    if tname == "web_search" and isinstance(args, dict):
                        args = refine_web_search_args(user_text, args)
                    if tname == "send_message" and isinstance(args, dict):
                        args = refine_send_message_args(user_text, args)
                    print(f"[JARVIS] 📞 {tname}")
                    out = await run_jarvis_tool(
                        tname,
                        args,
                        ui=self.ui,
                        speak=self.speak,
                        speak_error=self.speak_error,
                        loop=loop,
                        speak_from_tools=False,
                        user_query=user_text,
                    )
                    tool_body = _ollama_tool_message_body(tname, out)
                    tool_entry: dict = {
                        "role": "tool",
                        "content": tool_body,
                    }
                    if tname:
                        tool_entry["name"] = tname
                    tid = tc.get("id")
                    if tid:
                        tool_entry["tool_call_id"] = str(tid)
                    messages.append(tool_entry)
                if only_vision:
                    self.ui.write_log(
                        "SYS: Vision running in background — skipping extra chat reply "
                        "(Jarvis (vision) will speak when ready)."
                    )
                    return
                continue

            if content:
                self.ui.write_log(f"Jarvis: {content}")
                await self._speak_progressive(content)
            return

        self.ui.write_log("Jarvis: (no response after tool rounds)")
        await self._speak_progressive("Sir, I hit an internal reasoning limit on that request.")

    async def run(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._user_queue = asyncio.Queue()
        self._wire_text_input()

        win = getattr(self.ui, "_win", None)
        if win is not None and hasattr(win, "ollama_attached"):
            win.ollama_attached.emit(self)

        print(
            f"[JARVIS] 🦙 Local Ollama mode — {get_ollama_url()} model={get_ollama_model()}"
        )
        print("[JARVIS] ℹ️  Type commands, upload files, or use hold-to-talk (PTT) in the UI.")

        while True:
            try:
                self.ui.set_state("LISTENING")
                self.ui.write_log("SYS: JARVIS online (local Ollama).")
                if not self._coqui_preload_started:
                    self._coqui_preload_started = True

                    def _preload_coqui() -> None:
                        try:
                            from mark_coqui_tts import preload_coqui_engine

                            preload_coqui_engine()
                        except Exception as ex:
                            print(f"[TTS] Coqui preload: {ex}")

                    threading.Thread(target=_preload_coqui, daemon=True).start()
                while True:
                    item: Any = await self._user_queue.get()
                    try:
                        if (
                            isinstance(item, tuple)
                            and len(item) == 3
                            and item[0] == "pcm"
                        ):
                            _, pcm, sr = item
                            if not pcm or len(pcm) < 3200:
                                self.ui.write_log("SYS: PTT clip too short — ignored.")
                                continue
                            self.ui.set_state("THINKING")
                            self.ui.write_log("SYS: Transcribing (Whisper)…")
                            text = await asyncio.to_thread(
                                mark_voice.transcribe_pcm_int16,
                                pcm,
                                int(sr),
                            )
                            text = (text or "").strip()
                            if not text:
                                self.ui.write_log("SYS: No speech detected.")
                                if not self.ui.muted:
                                    self.ui.set_state("LISTENING")
                                continue
                            self.ui.write_log(f"You (PTT): {text}")
                            await self._run_one_exchange(text)
                            continue

                        user_text = str(item or "").strip()
                        if not user_text:
                            continue
                        await self._run_one_exchange(user_text)
                    except Exception as ex:
                        short = str(ex).strip()[:400]
                        self.ui.write_log(f"ERR: {short}")
                        traceback.print_exc()
                        if not self.ui.muted:
                            self.ui.set_state("LISTENING")
            except Exception as e:
                print(f"[JARVIS] ⚠️ {e}")
                traceback.print_exc()
            self.ui.set_state("THINKING")
            print("[JARVIS] 🔄 Reconnecting local loop in 3s...")
            await asyncio.sleep(3)
