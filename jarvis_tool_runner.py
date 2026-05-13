"""Shared async tool execution for Gemini Live and local Ollama backends."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import traceback
from typing import Any, Callable, Optional

from memory.memory_manager import update_memory

from actions.browser_control import browser_control
from actions.code_helper import code_helper
from actions.computer_control import computer_control
from actions.computer_settings import computer_settings
from actions.desktop import desktop_control
from actions.dev_agent import dev_agent
from actions.file_controller import file_controller
from actions.file_processor import file_processor
from actions.flight_finder import flight_finder
from actions.game_updater import game_updater
from actions.open_app import open_app
from actions.reminder import reminder
from actions.screen_processor import screen_process
from actions.send_message import send_message
from actions.weather_report import weather_action
from actions.web_search import web_search as web_search_action
from actions.youtube_video import youtube_video

import playbook_runner

SpeakFn = Callable[[str], None]
SpeakErrFn = Callable[[str, str], None]

# Console preview only; full tool payloads still go back to the LLM unchanged.
_TOOL_RESULT_LOG_CHARS = 400


def user_text_implies_external_messaging(user_text: str | None) -> bool:
    """
    True when the user clearly asked to use WhatsApp/SMS/etc., not only to speak
    a greeting aloud.
    """
    if not user_text:
        return False
    t = user_text.strip().lower()
    if not t:
        return False
    if re.search(
        r"(?i)\b("
        r"whatsapp|telegram|signal|slack|discord|imessage|sms|fb\s+messenger|facebook\s+messenger"
        r")\b",
        t,
    ):
        return True
    if re.search(r"(?i)\bon\s+(?:whatsapp|telegram|signal|slack|discord)\b", t):
        return True
    if re.search(
        r"(?i)\b(send|fire)\s+(?:him|her|them\s+)?(?:a\s+)?(?:text|message|dm)\b", t
    ):
        return True
    if re.search(r"(?i)\bsend\s+(?:him|her|them\s+)?(?:a\s+)?(?:text\s+)?message\b", t):
        return True
    if re.search(r"(?i)\bsend\s+(?:a\s+)?text\s+to\b", t):
        return True
    if re.search(r"(?i)\bmessage\s+(?:him|her|them)\s+on\b", t):
        return True
    if re.search(r"(?i)\bdm\b", t):
        return True
    if re.search(r"(?i)\b(text|ping)\s+(?:him|her|them)\s+on\b", t):
        return True
    # Webmail / email outbound (Proton, Gmail, …) — not covered by SMS/WhatsApp patterns above.
    if re.search(r"(?i)\b(send|fire|write|compose)\b.*\b(email|e-mail)\b", t):
        return True
    if re.search(r"(?i)\b(email|e-mail)\b.*\bto\b", t) and (
        "@" in t
        or re.search(
            r"(?i)\b[\w.+-]+@(gmail|outlook|yahoo|hotmail|icloud|proton)\.",
            t,
        )
    ):
        return True
    if re.search(r"(?i)\bsend\b.*@", t):
        return True
    if re.search(
        r"(?i)\b("
        r"proton\s*mail|protonmail|gmail|google\s+mail|outlook|hotmail|yahoo(?:mail)?|icloud"
        r")\b.*\b(send|compose|email|write|mail)\b",
        t,
    ):
        return True
    if re.search(
        r"(?i)\b(send|compose|email|write|mail)\b.*\b("
        r"proton\s*mail|protonmail|gmail|google\s+mail|outlook|hotmail|yahoo|icloud"
        r")\b",
        t,
    ):
        return True
    return False


def user_text_is_in_character_greeting_only(user_text: str | None) -> bool:
    """
    Phrases like \"say hi to my grandson Cayden\" are meant as spoken roleplay,
    not desktop automation to WhatsApp.
    """
    if not user_text:
        return False
    u = user_text.strip()
    if len(u) > 400:
        return False
    tl = u.lower()
    if user_text_implies_external_messaging(u):
        return False
    patterns = (
        r"(?i)\bcan\s+you\s+say\s+(?:hi|hello|hey)\s+to\b",
        r"(?i)\bplease\s+say\s+(?:hi|hello|hey)\s+to\b",
        r"(?i)\bsay\s+(?:hi|hello|hey)\s+to\b",
        r"(?i)\bgive\s+(?:him|her|them\s+)?(?:a\s+)?(?:wave\s+and\s+)?(?:hi|hello|hey)\s+to\b",
        r"(?i)\bpass\s+(?:along\s+)?(?:a\s+)?(?:hello|hi|hey)\s+to\b",
        r"(?i)\btell\s+\w[\w'-]*\s+(?:hi|hello|hey)\b",
        r"(?i)\btell\s+\w[\w'-]*\s+that\s+i\s+said\s+hi\b",
        r"(?i)\btell\s+\w[\w'-]*\s+i\s+said\s+hi\b",
    )
    return any(re.search(p, tl) for p in patterns)


def user_text_explicitly_requests_real_message(user_text: str | None) -> bool:
    """
    Require clear outbound intent before allowing ``send_message`` execution.
    """
    if not user_text:
        return False
    u = user_text.strip()
    if not u:
        return False
    if user_text_is_in_character_greeting_only(u):
        return False
    return user_text_implies_external_messaging(u)


def _user_text_requests_recurring_reminder(user_text: str) -> bool:
    """Explicit phrases that mean a repeating schedule (not a single fire)."""
    t = (user_text or "").lower().strip()
    if not t:
        return False
    patterns = (
        r"\b(every|each)\s+day\b",
        r"\bevery\s+morning\b",
        r"\bevery\s+night\b",
        r"\bevery\s+evening\b",
        r"\bevery\s+afternoon\b",
        r"\bdaily\b",
        r"\bweekdays?\b",
        r"\bweekly\b",
        r"\bevery\s+week\b",
        r"\bmonday\s+through\s+friday\b",
        r"\brepeat(ing|s)?\b",
        r"\brecurr(ing|ent|ence)?\b",
        r"\ball\s+weekdays\b",
    )
    return any(re.search(p, t) for p in patterns)


def _user_text_suggests_one_shot_reminder(user_text: str) -> bool:
    """
    Phrases like \"tomorrow at 9\" or a lone \"alarm\" request are usually one-shot,
    not ``recurrence: daily`` in Task Scheduler.
    """
    t = (user_text or "").lower().strip()
    if not t:
        return False
    if re.search(
        r"\b(tomorrow|tonight|later\s+today|this\s+evening|"
        r"next\s+(?:mon(?:day)?|tues(?:day)?|wed(?:nesday)?|thu(?:rsday)?|"
        r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)|"
        r"next\s+week\b|one[\s-]?time|just\s+once|only\s+once|single\s+time)\b",
        t,
    ):
        return True
    if re.search(r"\balarm\b", t) and not re.search(
        r"\b(every|each|daily|repeat|recurr|weekday|weekly)\b",
        t,
    ):
        return True
    return False


def _coerce_computer_control_open_app_to_open_app(name: str, args: dict) -> tuple[str, dict]:
    """
    Local models sometimes emit ``computer_control`` with ``action: \"open_app\"`` instead of
    the real ``open_app`` tool — that only logs ``[Computer] open_app`` and does not launch.
    """
    if name != "computer_control" or not isinstance(args, dict):
        return name, args
    act = (args.get("action") or "").strip().lower()
    if act not in ("open_app", "launch_app", "start_app"):
        return name, args
    app = (
        (args.get("app_name") or args.get("application") or args.get("name") or "")
        .strip()
    )
    if not app:
        app = "Notepad"
    print(f"[JARVIS] rerouting computer_control(action={act!r}) → open_app({app!r})")
    return "open_app", {"app_name": app}


def _open_app_compose_followup_hint(user_query: str | None, app_name: str) -> str:
    """
    When Notepad (or similar) is opened and the user asked to *write* / *compose* content,
    small models often stop after ``open_app``. Append a strict instruction to the tool
    result so the next assistant step calls ``computer_control`` ``smart_type``.
    """
    if not (user_query or "").strip():
        return ""
    uq = user_query.lower()
    app = (app_name or "").lower()
    simple = ("notepad", "textedit", "gedit", "wordpad")
    notepadish = any(s in app for s in simple) or bool(
        re.search(r"\b(word|winword|writer|libreoffice)\b", app)
    )
    wants_text = bool(
        re.search(
            r"\b(write|type|compose|dictate|draft|put|add|fill|create)\b",
            uq,
        )
    ) or bool(
        re.search(
            r"\b(poem|poetry|story|stories|letter|letters|note|notes|paragraph|essay|"
            r"message|lyrics|speech|toast|vows|rap|haiku|limerick)\b",
            uq,
        )
    )
    if not notepadish or not wants_text:
        return ""
    return (
        " HOST_INSTRUCT: The user asked for **your** original text in this editor. The **host** "
        "waits briefly after this launch before running the **next** tool in the same batch so "
        "the window is ready. Call **computer_control** next with **action: smart_type** and "
        "**text** = the **full** composed content (same turn). **Do not** use **computer_control** "
        "**action: wait** before typing — it often pulls focus away from the document. **Do not** "
        "refuse or say you cannot control applications. **Do not** paste the poem only into chat. "
        "After typing, one short courteous line with **thank you** is enough."
    )


def refine_reminder_date_from_user_text(user_text: str, args: dict) -> dict:
    """
    Models often pass **today's** ``date`` when the user said **tomorrow** (or **day after
    tomorrow**). Nudge ``date`` forward when the words and the calendar disagree.
    """
    from datetime import datetime, timedelta

    if not isinstance(args, dict):
        return args
    date_str = (args.get("date") or "").strip()
    if not date_str:
        return args
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return args

    t = (user_text or "").lower().strip()
    if not t:
        return args

    today = datetime.now().date()
    out = dict(args)

    def set_date(new_d, old: str, reason: str) -> None:
        ns = new_d.strftime("%Y-%m-%d")
        print(f"[JARVIS] reminder: date {old!r} -> {ns!r} ({reason}).")
        out["date"] = ns

    # Do not bump when they clearly anchored on "today" / tonight.
    user_said_today = bool(
        re.search(r"\b(today|tonight|this\s+morning|this\s+afternoon|this\s+evening)\b", t)
    )

    if re.search(r"\bthe\s+day\s+after\s+tomorrow\b", t):
        want = today + timedelta(days=2)
        if parsed < want:
            set_date(want, date_str, "user said day after tomorrow")
        return out

    if re.search(r"\btomorrow\b", t) and not user_said_today:
        want = today + timedelta(days=1)
        if parsed < want:
            set_date(want, date_str, "user said tomorrow")
        return out

    return out


def refine_reminder_args(user_text: str, args: dict) -> dict:
    """
    Align ``recurrence`` with what the user said (models often emit ``daily`` for
    \"tomorrow at 9am\"). Fix **date** when the user said tomorrow but the model used
    today. Does not change **list** / **cancel**.
    """
    if not isinstance(args, dict):
        return args
    action = (args.get("action") or "schedule").strip().lower()
    if action != "schedule":
        return args

    u = (user_text or "").strip()
    out = dict(args)

    rec = (out.get("recurrence") or "once").strip().lower()
    if rec != "once" and u:
        if not _user_text_requests_recurring_reminder(u) and _user_text_suggests_one_shot_reminder(
            u
        ):
            print(
                f"[JARVIS] reminder: recurrence {rec!r} -> once (user text looks one-shot, "
                "not recurring)."
            )
            out["recurrence"] = "once"
            out.pop("job_name", None)

    if u:
        out = refine_reminder_date_from_user_text(u, out)

    return out


async def run_jarvis_tool(
    name: str,
    args: dict,
    *,
    ui,
    speak: SpeakFn,
    speak_error: SpeakErrFn,
    loop: asyncio.AbstractEventLoop,
    speak_from_tools: bool = True,
    user_query: str | None = None,
) -> dict[str, Any]:
    """
    Execute one JARVIS tool by name.

    Returns a dict: ``{"result": str|...}`` or ``{"result": "ok", "silent": True}`` for save_memory.

    ``speak_from_tools``: when False, tools that would TTS (e.g. ``weather_report``) stay silent
    so the host can speak only the model follow-up (avoids double audio on local Ollama).

    ``user_query``: original user text for this turn (used to block mistaken ``send_message``
    when they only asked for an in-character greeting).
    """
    print(f"[JARVIS] 🔧 {name}  {args}")
    ui.set_state("THINKING")

    if name == "save_memory":
        category = args.get("category", "notes")
        key = args.get("key", "")
        value = args.get("value", "")
        if key and value:
            update_memory({category: {key: {"value": value}}})
            print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
        if not ui.muted:
            ui.set_state("LISTENING")
        return {"result": "ok", "silent": True}

    result: str = "Done."

    try:
        if name == "open_app":
            uq = (user_query or "").strip() if user_query else ""
            app_name_l = str(args.get("app_name", "")).strip().lower()
            if uq and user_text_requests_proton_read_screen(uq) and "proton" in app_name_l:
                if hasattr(ui, "write_log"):
                    ui.write_log(
                        "SYS: Rerouting open_app(Proton...) -> run_capability(proton_mail/open_native) (simple open-first flow)."
                    )
                r = await loop.run_in_executor(
                    None,
                    lambda: playbook_runner.run_capability(
                        {
                            "capability_id": "proton_mail",
                            "action": "open_native",
                        },
                        player=ui,
                    ),
                )
                result = r or "Done."
            elif (
                uq
                and user_text_requests_proton_open(uq)
                and user_text_mentions_browser(uq)
                and "proton" in app_name_l
            ):
                if hasattr(ui, "write_log"):
                    ui.write_log(
                        "SYS: Rerouting open_app(Proton app) -> run_capability(proton_mail/open_native) because browser was requested."
                    )
                r = await loop.run_in_executor(
                    None,
                    lambda: playbook_runner.run_capability(
                        {
                            "capability_id": "proton_mail",
                            "action": "open_native",
                        },
                        player=ui,
                    ),
                )
                result = r or "Done."
            else:
                r = await loop.run_in_executor(
                    None, lambda: open_app(parameters=args, response=None, player=ui)
                )
                result = (r or f"Opened {args.get('app_name')}.") + _open_app_compose_followup_hint(
                    user_query, str(args.get("app_name", ""))
                )

        elif name == "computer_control":
            cname, cargs = _coerce_computer_control_open_app_to_open_app(
                name, args if isinstance(args, dict) else {}
            )
            if cname == "open_app":
                r = await loop.run_in_executor(
                    None, lambda: open_app(parameters=cargs, response=None, player=ui)
                )
                result = (r or f"Opened {cargs.get('app_name')}.") + _open_app_compose_followup_hint(
                    user_query, str(cargs.get("app_name", ""))
                )
            else:
                r = await loop.run_in_executor(
                    None, lambda: computer_control(parameters=args, player=ui)
                )
                result = r or "Done."

        elif name == "weather_report":
            tool_speak = speak if speak_from_tools else None
            r = await loop.run_in_executor(
                None,
                lambda: weather_action(
                    parameters=args, player=ui, speak=tool_speak
                ),
            )
            result = r or "Weather delivered."

        elif name == "browser_control":
            uq = (user_query or "").strip() if user_query else ""
            bargs = args if isinstance(args, dict) else {}
            action_now = str(bargs.get("action", "")).strip().lower()
            url_now = str(bargs.get("url", "")).strip()
            if uq and user_text_requests_proton_read_screen(uq):
                if hasattr(ui, "write_log"):
                    ui.write_log(
                        "SYS: Rerouting browser_control(Proton read intent) -> run_capability(proton_mail/open_native) (simple open-first flow)."
                    )
                r = await loop.run_in_executor(
                    None,
                    lambda: playbook_runner.run_capability(
                        {
                            "capability_id": "proton_mail",
                            "action": "open_native",
                        },
                        player=ui,
                    ),
                )
                result = r or "Done."
            elif uq and user_text_requests_proton_open(uq):
                if hasattr(ui, "write_log"):
                    ui.write_log(
                        "SYS: Rerouting browser_control(Proton open intent) -> run_capability(proton_mail/open_native)."
                    )
                r = await loop.run_in_executor(
                    None,
                    lambda: playbook_runner.run_capability(
                        {
                            "capability_id": "proton_mail",
                            "action": "open_native",
                        },
                        player=ui,
                    ),
                )
                result = r or "Done."
            else:
                if action_now == "go_to" and _looks_like_proton_url(url_now):
                    bargs = {**bargs, "browser": "edge"}
                r = await loop.run_in_executor(
                    None, lambda: browser_control(parameters=bargs, player=ui)
                )
                r_text = (r or "").strip()
                # Baseline recovery: if model issued browser_control without a session,
                # open Edge directly via Start-menu behavior so "open browser" always works.
                if "no active browser sessions" in r_text.lower():
                    wants_open_browser = bool(
                        re.search(
                            r"\b(open|launch|start)\b.*\b(browser|edge|web)\b",
                            uq.lower(),
                        )
                    )
                    if wants_open_browser:
                        opened = await loop.run_in_executor(
                            None,
                            lambda: open_app(
                                parameters={"app_name": "edge"},
                                response=None,
                                player=ui,
                            ),
                        )
                        result = opened or "Opened Edge."
                    else:
                        result = r or "Done."
                else:
                    result = r or "Done."

        elif name == "file_controller":
            r = await loop.run_in_executor(
                None, lambda: file_controller(parameters=args, player=ui)
            )
            result = r or "Done."

        elif name == "send_message":
            uq = (user_query or "").strip() if user_query else ""
            if uq and not user_text_explicitly_requests_real_message(uq):
                result = (
                    "SKIPPED — Host policy: outbound messaging was blocked because your request "
                    "did not explicitly ask to send/text/message/email."
                )
                if hasattr(ui, "write_log"):
                    ui.write_log(
                        "SYS: send_message skipped — explicit outbound intent required."
                    )
                print("[JARVIS] send_message skipped (explicit outbound intent missing).")
            else:
                r = await loop.run_in_executor(
                    None,
                    lambda: send_message(
                        parameters=args, response=None, player=ui, session_memory=None
                    ),
                )
                result = r or f"Message sent to {args.get('receiver')}."

        elif name == "reminder":
            refined = (
                refine_reminder_args(user_query or "", args)
                if isinstance(args, dict)
                else args
            )
            r = await loop.run_in_executor(
                None,
                lambda p=refined: reminder(parameters=p, response=None, player=ui),
            )
            result = r or "Reminder set."

        elif name == "youtube_video":
            r = await loop.run_in_executor(
                None, lambda: youtube_video(parameters=args, response=None, player=ui)
            )
            result = r or "Done."

        elif name == "run_capability":
            rq = (user_query or "").strip() if user_query else ""
            rargs = args if isinstance(args, dict) else {}
            cid = str(rargs.get("capability_id") or rargs.get("capability") or "").strip().lower()
            if cid == "proton_mail" and user_text_requests_proton_read_screen(rq):
                action_now = str(rargs.get("action") or "").strip().lower()
                if action_now in ("", "open", "inbox"):
                    rargs = {**rargs, "action": "open_native"}
            if cid == "proton_mail" and user_text_requests_proton_open(rq):
                action_now = str(rargs.get("action") or "").strip().lower()
                if action_now in ("", "open", "inbox"):
                    rargs = {**rargs, "action": "open_native"}
            r = await loop.run_in_executor(
                None,
                lambda: playbook_runner.run_capability(
                    rargs,
                    player=ui,
                ),
            )
            result = r or "Done."

        elif name == "screen_process":
            threading.Thread(
                target=screen_process,
                kwargs={
                    "parameters": args,
                    "response": None,
                    "player": ui,
                    "session_memory": None,
                },
                daemon=True,
            ).start()
            result = (
                "Vision module activated. Stay completely silent — "
                "vision module will speak directly."
            )

        elif name == "computer_settings":
            r = await loop.run_in_executor(
                None,
                lambda: computer_settings(parameters=args, response=None, player=ui),
            )
            result = r or "Done."

        elif name == "desktop_control":
            r = await loop.run_in_executor(
                None, lambda: desktop_control(parameters=args, player=ui)
            )
            result = r or "Done."

        elif name == "code_helper":
            r = await loop.run_in_executor(
                None, lambda: code_helper(parameters=args, player=ui, speak=speak)
            )
            result = r or "Done."

        elif name == "dev_agent":
            r = await loop.run_in_executor(
                None, lambda: dev_agent(parameters=args, player=ui, speak=speak)
            )
            result = r or "Done."

        elif name == "agent_task":
            from agent.task_queue import TaskPriority, get_queue

            priority_map = {
                "low": TaskPriority.LOW,
                "normal": TaskPriority.NORMAL,
                "high": TaskPriority.HIGH,
            }
            priority = priority_map.get(
                args.get("priority", "normal").lower(), TaskPriority.NORMAL
            )
            task_id = get_queue().submit(
                goal=args.get("goal", ""), priority=priority, speak=speak
            )
            result = f"Task started (ID: {task_id})."

        elif name == "web_search":
            uq = (user_query or "").strip() if user_query else ""
            maybe_url = str(args.get("url", "")).strip().lower() if isinstance(args, dict) else ""
            is_proton_url = ("protonmail.com" in maybe_url) or ("mail.proton.me" in maybe_url)
            if uq and user_text_requests_proton_read_screen(uq) and (
                is_proton_url or ("proton" in uq.lower())
            ):
                if hasattr(ui, "write_log"):
                    ui.write_log(
                        "SYS: Rerouting web_search(Proton) -> run_capability(proton_mail/open_native) (simple open-first flow)."
                    )
                r = await loop.run_in_executor(
                    None,
                    lambda: playbook_runner.run_capability(
                        {
                            "capability_id": "proton_mail",
                            "action": "open_native",
                        },
                        player=ui,
                    ),
                )
                result = r or "Done."
            else:
                r = await loop.run_in_executor(
                    None, lambda: web_search_action(parameters=args, player=ui)
                )
                result = r or "Done."

        elif name == "file_processor":
            if not args.get("file_path") and ui.current_file:
                args = {**args, "file_path": ui.current_file}
            r = await loop.run_in_executor(
                None,
                lambda: file_processor(parameters=args, player=ui, speak=speak),
            )
            result = r or "Done."

        elif name == "game_updater":
            r = await loop.run_in_executor(
                None, lambda: game_updater(parameters=args, player=ui, speak=speak)
            )
            result = r or "Done."

        elif name == "flight_finder":
            r = await loop.run_in_executor(
                None, lambda: flight_finder(parameters=args, player=ui)
            )
            result = r or "Done."

        elif name == "shutdown_jarvis":
            ui.write_log("SYS: Shutdown requested.")
            speak("Goodbye, sir.")

            def _shutdown() -> None:
                import os
                import time

                time.sleep(1)
                os._exit(0)

            threading.Thread(target=_shutdown, daemon=True).start()

        else:
            result = f"Unknown tool: {name}"

    except Exception as e:
        result = f"Tool '{name}' failed: {e}"
        traceback.print_exc()
        speak_error(name, str(e))

    if not ui.muted:
        ui.set_state("LISTENING")

    _out = str(result)
    _lim = _TOOL_RESULT_LOG_CHARS
    if len(_out) > _lim:
        print(f"[JARVIS] 📤 {name} → {_out[:_lim]}… ({len(_out)} chars total, truncated for terminal)")
    else:
        print(f"[JARVIS] 📤 {name} → {_out}")
    return {"result": result}


def ollama_tools_from_gemini_declarations(
    declarations: list[dict],
) -> list[dict]:
    """Convert Gemini-style function_declarations to Ollama/OpenAI-style tools."""

    def norm_schema(node: object) -> object:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "type" and isinstance(v, str):
                    t = v.upper()
                    mapped = {
                        "OBJECT": "object",
                        "STRING": "string",
                        "INTEGER": "integer",
                        "NUMBER": "number",
                        "BOOLEAN": "boolean",
                        "ARRAY": "array",
                    }.get(t, v.lower() if t.isupper() and len(t) > 1 else v)
                    out[k] = mapped
                else:
                    out[k] = norm_schema(v)
            return out
        if isinstance(node, list):
            return [norm_schema(x) for x in node]
        return node

    tools: list[dict] = []
    for decl in declarations:
        params = decl.get("parameters") or {"type": "object", "properties": {}}
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": decl["name"],
                    "description": decl.get("description", ""),
                    "parameters": norm_schema(params),
                },
            }
        )
    return tools


def parse_tool_arguments(raw: object) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _balanced_json_slice(text: str, open_brace: int) -> Optional[str]:
    """Return the JSON object starting at ``open_brace``, or ``None`` if unbalanced."""
    if open_brace < 0 or open_brace >= len(text) or text[open_brace] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    quote_char = ""
    for j in range(open_brace, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote_char:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote_char = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace : j + 1]
    return None


def _same_line_prefix_before_brace(text: str, open_brace_idx: int) -> str:
    """Text on the same line as ``text[open_brace_idx] == '{'``, before that brace."""
    if open_brace_idx < 0 or open_brace_idx > len(text):
        return ""
    line_start = text.rfind("\n", 0, open_brace_idx) + 1
    return text[line_start:open_brace_idx]


_STRICT_MULTILINE_SYNTHETIC = frozenset(
    {"send_message", "open_app", "weather_report", "run_capability"}
)
# OpenAI-style JSON tools with ``"arguments": {}`` — empty dict is falsy but valid here.
_ALLOW_EMPTY_SYNTHETIC_JSON_ARGS = frozenset({"weather_report"})

# Leading "Yeah, …" / "Yes, …" from follow-up turns (bad as a literal search query).
_AFFIRMATIVE_LEAD_IN = re.compile(
    r"(?i)^\s*(?:yeah|yep|yes|sure|ok|okay|right|absolutely|correct|fine)\s*[,!.:]\s*"
)


def scrub_affirmative_lead(text: str) -> str:
    """Strip one or more leading affirmative fillers (``Yeah, `` …) from a line."""
    t = (text or "").strip()
    guard = 0
    while t and guard < 6:
        guard += 1
        nxt = _AFFIRMATIVE_LEAD_IN.sub("", t, count=1).strip()
        if nxt == t:
            break
        t = nxt
    return t


# Strip common PTT phrasing so we can compare the user's real topic to the model's query.
_DISTILL_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Must consume at least one character — avoid a zero-width match on ``^`` alone.
    re.compile(r"(?i)^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|will\s+you\s+)+"),
    re.compile(
        r"(?i)^\s*tell\s+me\s+(?:what\s+you\s+think\s+(?:about|of)\s+|about)\s+"
    ),
    # "Use web search for" / "Use the web search for" (not only "Use the web …").
    re.compile(
        r"(?i)^\s*(?:use\s+(?:the\s+)?)?web\s*search\s*,?\s*(for|to|about)\s+"
    ),
    re.compile(r"(?i)^\s*do\s+a\s+web\s*search\s+(about|for|on)\s+"),
    re.compile(r"(?i)^\s*search\s+the\s+web\s*,?\s*(for|on)?\s*"),
    re.compile(r"(?i)^\s*search\s+"),
    re.compile(r"(?i)^\s*look\s+up\s+"),
    re.compile(r"(?i)^\s*google\s+"),
)

_STOP_SEARCH_TOKENS = frozenset(
    {
        "the",
        "and",
        "for",
        "you",
        "any",
        "use",
        "web",
        "search",
        "today",
        "news",
        "headline",
        "headlines",
        "about",
        "latest",
        "some",
        "into",
        "pull",
        "article",
        "articles",
        "developments",
        "recent",
        "with",
        "from",
        "that",
        "this",
        "have",
        "been",
        "there",
        "doesnt",
        "dont",
    }
)

_BROAD_NEWS_HINTS = (
    "top us news",
    "top news today",
    "latest news",
    "headlines today",
    "breaking news",
    "us news today",
    "national news",
)


def distill_user_search_intent(user_text: str) -> str:
    """Remove leading 'web search for …' style boilerplate from the user line."""
    s = scrub_affirmative_lead(user_text or "")
    if not s:
        return ""
    changed = True
    guard = 0
    while changed and s and guard < 12:
        guard += 1
        changed = False
        for pat in _DISTILL_PREFIX_PATTERNS:
            ns, nsub = pat.subn("", s, count=1)
            if nsub:
                s = ns.strip()
                changed = True
                break
    s = s.strip().rstrip("?.!").strip()
    return s[:280]


def _meaningful_tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9'.-]{2,}", text or "")
        if t.lower() not in _STOP_SEARCH_TOKENS
    }


def _is_broad_news_query(q_lower: str) -> bool:
    return any(h in q_lower for h in _BROAD_NEWS_HINTS)


def refine_web_search_query(user_text: str, model_query: str) -> str:
    """If the model picked a generic news query but the user named a topic, prefer the user."""
    mq = (model_query or "").strip()
    distilled = distill_user_search_intent(user_text or "")
    if not distilled:
        return mq
    if not mq:
        return distilled[:280]
    mq_l = mq.lower()
    d_toks = _meaningful_tokens(distilled)
    m_toks = _meaningful_tokens(mq)
    if not d_toks:
        return mq
    missing = d_toks - m_toks
    if missing and (_is_broad_news_query(mq_l) or len(missing) >= 2):
        return distilled[:280]
    return mq


def compact_vague_news_web_query(query: str, context: str) -> str:
    """
    Turn spoken news questions into tight DDG keywords (full sentences often return nothing).
    """
    q = (query or "").strip()
    if not q:
        return q
    combined = f"{q} {context or ''}".lower()
    if re.search(
        r"(?i)\b(weather|temperature|forecast|rain|snow|humidity|mph|degrees)\b",
        combined,
    ):
        return q
    newsish = bool(
        re.search(
            r"(?i)\b(news|headlines|breaking|international|worldwide|world|globe|"
            r"going\s+on|happening|today|current\s+events)\b",
            combined,
        )
    )
    vague_phrase = bool(
        re.search(
            r"(?i)\b(what'?s?\s+going\s+on|what\s+is\s+happening|what\s+happened|"
            r"what'?s?\s+the\s+news|what'?s?\s+new|what\s+are\s+the\s+headlines|"
            r"anything\s+important\s+in\s+the\s+news|in\s+the\s+news\s+today)\b",
            q,
        )
    )
    long_what = bool(
        len(q) > 44
        and re.match(r"(?i)^(what|when|where|why|how|who|tell\s+me|give\s+me)\b", q)
        and newsish
    )
    if not (vague_phrase or long_what):
        return q
    if "iran" in combined:
        return "Iran international news headlines today"
    if re.search(r"\b(us|u\.s\.|america|american|washington)\b", combined):
        return "US news headlines today"
    if re.search(
        r"\b(world|global|international|globe|everywhere|planet|earth)\b",
        combined,
    ):
        return "world news headlines today"
    if newsish:
        return "top world news headlines today"
    return q


def apply_site_operator_from_user_request(user_text: str, query: str) -> str:
    """Add site: when the user asked for a specific outlet."""
    u = user_text or ""
    ul = u.lower()
    q = (query or "").strip()
    if not q or "site:" in q.lower():
        return q
    if "rt.com" in ul or re.search(r"\brt\.com\b", u, re.I) or re.search(
        r"(?i)\brt\s+news\b", u
    ):
        q_body = re.sub(r"(?i)^\s*rt\.com\s+", "", q).strip()
        return f"site:rt.com {q_body}"[:280]
    if "apnews.com" in ul or "apnews" in ul or re.search(r"\bap\s+news\b", u, re.I):
        q_body = re.sub(r"(?i)^\s*apnews\.com\s+", "", q).strip()
        return f"site:apnews.com {q_body}"[:280]
    if re.search(r"\breuters\b", ul):
        q_body = re.sub(r"(?i)^\s*reuters\.com\s+", "", q).strip()
        return f"site:reuters.com {q_body}"[:280]
    return q


def refine_web_search_args(user_text: str, args: dict) -> dict:
    """Blend user intent + optional site: hint into web_search parameters."""
    if not isinstance(args, dict):
        return args
    mode = (args.get("mode") or "").strip().lower()
    if mode == "fetch":
        return args
    u = scrub_affirmative_lead((user_text or "").strip())
    raw_q = (args.get("query") or "").strip()
    # If the model emitted ``web_search`` with no query, fall back to the user's words.
    if not raw_q and u:
        raw_q = u
        args = {**args, "query": raw_q}
    # News mode should still be normalized to a concrete broad query when vague.
    if mode == "news":
        q_news = compact_vague_news_web_query(raw_q, u or raw_q)
        if u:
            q_news = apply_site_operator_from_user_request(u, q_news)
        if q_news != raw_q:
            print(f"[JARVIS] web_search(news) query refined: {raw_q!r} -> {q_news!r}")
            return {**args, "query": q_news}
        return args
    ctx = u or raw_q
    q = scrub_affirmative_lead(raw_q)
    if u:
        q = refine_web_search_query(u, q)
    # Run *after* topic refinement so we do not replace a tight query with long distilled text.
    q = compact_vague_news_web_query(q, ctx)
    if u:
        q = apply_site_operator_from_user_request(u, q)
    if q == raw_q:
        return args
    print(f"[JARVIS] web_search query refined: {raw_q!r} -> {q!r}")
    return {**args, "query": q}


def infer_send_message_platform_from_user_text(user_text: str) -> str:
    """
    When the model omits ``platform`` (common with plain-text pseudo tool calls),
    derive the desktop app from the user's words. Returns ``\"\"`` if unclear.
    """
    u = (user_text or "").strip().lower()
    if not u:
        return ""

    def _has(*phrases: str) -> bool:
        return any(p in u for p in phrases)

    # Email clients (check before generic "mail")
    if "protonmail" in u.replace(" ", "") or _has("proton mail"):
        return "Proton Mail"
    if re.search(r"\bproton\b", u) and _has("mail", "email"):
        return "Proton Mail"
    if _has("gmail", "google mail"):
        return "Gmail"
    if _has("outlook", "hotmail", "live.com", "office 365 mail"):
        return "Outlook"
    if _has("thunderbird"):
        return "Thunderbird"

    if _has("whatsapp", "whats app"):
        return "WhatsApp"
    if _has("telegram"):
        return "Telegram"
    if re.search(r"\bsignal\b", u):
        return "Signal"
    if _has("discord"):
        return "Discord"
    if "instagram" in u or re.search(r"\binsta\b", u):
        return "Instagram"
    if _has("messenger", "facebook message", "fb messenger"):
        return "Messenger"

    if _has("email", "e-mail") and not re.search(
        r"\b(slack|teams|zoom|sms|text|dm)\b", u
    ):
        # Generic "send an email" with no named client — still ambiguous for automation.
        return ""

    return ""


def user_text_requests_proton_read_screen(user_text: str) -> bool:
    """True when the user asks to read/summarize Proton inbox content on screen."""
    u = (user_text or "").strip().lower()
    if not u:
        return False
    compact = u.replace(" ", "")
    mentions_proton = ("protonmail" in compact) or ("proton mail" in u) or (
        ("proton" in u) and ("mail" in u or "email" in u or "inbox" in u)
    )
    if not mentions_proton:
        return False
    asks_read = bool(
        re.search(
            r"\b(read|summari[sz]e|what(?:'s| is)\s+in|inbox|visible|on\s+screen|screen)\b",
            u,
        )
    )
    asks_send = bool(
        re.search(r"\b(send|compose|draft|reply|forward|email\s+to|message)\b", u)
    )
    return asks_read and not asks_send


def user_text_requests_proton_open(user_text: str) -> bool:
    """True when user asks to open/navigate Proton Mail (without read/send intent)."""
    u = (user_text or "").strip().lower()
    if not u:
        return False
    compact = u.replace(" ", "")
    mentions_proton = ("protonmail" in compact) or ("proton mail" in u) or (
        ("proton" in u) and ("mail" in u or "email" in u or "inbox" in u)
    )
    if not mentions_proton:
        return False
    asks_open = bool(re.search(r"\b(open|launch|start|go to|goto|navigate)\b", u))
    asks_read = bool(
        re.search(r"\b(read|summari[sz]e|what(?:'s| is)\s+in|on\s+screen|screen)\b", u)
    )
    asks_send = bool(
        re.search(r"\b(send|compose|draft|reply|forward|email\s+to|message)\b", u)
    )
    return asks_open and not asks_read and not asks_send


def user_text_mentions_browser(user_text: str) -> bool:
    u = (user_text or "").strip().lower()
    if not u:
        return False
    return bool(re.search(r"\b(browser|edge|web)\b", u))


def _looks_like_proton_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return ("mail.proton.me" in u) or ("protonmail.com" in u)


def refine_send_message_args(user_text: str, args: dict) -> dict:
    """Fill missing ``platform``, and coerce ``receiver`` to an email when the utterance contains one."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    plat = (out.get("platform") or "").strip()
    if not plat:
        inferred = infer_send_message_platform_from_user_text(user_text)
        if inferred:
            print(f"[JARVIS] send_message platform inferred from user text: {inferred!r}")
            out["platform"] = inferred

    recv = (out.get("receiver") or "").strip()
    ut = (user_text or "").strip()
    if ut:
        m = re.search(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            ut,
        )
        if m and (not recv or "@" not in recv):
            email = m.group(0)
            print(f"[JARVIS] send_message receiver set from user text email: {email!r}")
            out["receiver"] = email
    return out


def _line_smells_like_chat_prose(line: str) -> bool:
    """True for a normal sentence line (not JSON / not ``tool_name(``)."""
    t = (line or "").strip()
    if len(t) < 8:
        return False
    if t.startswith("{") or t.startswith("["):
        return False
    if t.lstrip().startswith("```"):
        return False
    if not t[0].isalpha():
        return False
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*[\(\{]", t):
        return False
    return True


def _allow_high_risk_synthetic_tool(content: str, tool_calls: list[dict]) -> bool:
    """
    Block hallucinated ``send_message`` / ``open_app`` / ``weather_report`` / ``run_capability``
    when the model mixes chat with a bare JSON tool line (e.g. user says \"Sports\" and the
    model emits unrelated ``weather_report`` JSON). Still allow explicit
    ``weather_report({...})`` / ``weather_report {`` lines.
    """
    if not tool_calls:
        return True
    fn = tool_calls[0].get("function") or {}
    name = fn.get("name")
    if name not in _STRICT_MULTILINE_SYNTHETIC:
        return True
    if not any(_line_smells_like_chat_prose(ln) for ln in content.splitlines()):
        return True
    if name == "send_message":
        return bool(re.search(r"^\s*send_message\s*[\(\{]", content, re.MULTILINE))
    if name == "open_app":
        return bool(re.search(r"^\s*open_app\s*[\(\{]", content, re.MULTILINE))
    if name == "weather_report":
        return bool(re.search(r"^\s*weather_report\s*[\(\{]", content, re.MULTILINE))
    if name == "run_capability":
        # Block prose + buried ``{"name":"run_capability",...}`` (common when models
        # explain Parse Syntax Grammar with JSON-looking examples).
        return bool(re.search(r"^\s*run_capability\s*[\(\{]", content, re.MULTILINE))
    return True


def synthetic_tool_calls_from_text(
    content: str,
    *,
    valid_names: set[str],
) -> list[dict]:
    """
    Some local models print tool intent in ``message.content`` instead of Ollama
    ``tool_calls``. Supported whole-message shapes:

    - ``open_app({"app_name": "Notepad"})``
    - ``weather_report(city: "Miami, FL")`` or ``weather_report(city="Miami")`` (kwargs, not JSON)
    - ``{"name": "weather_report", "arguments": {}}`` or ``weather_report({})`` (defaults / config cities)
    - ``[web_search(query=\"...\")]`` (bracket-wrapped pseudo-call)
    - ``open_app {"app_name": "Notepad"}`` (space instead of parentheses)
    - ``{"name": "open_app", "arguments": {"app_name": "Notepad"}}``
    - A tool line buried after prose (each non-empty line is tried).
    - JSON starting mid-string only if the same-line text before ``{`` is short
      (≤96 chars), so long chit-chat plus hallucinated tool JSON is ignored.
    - ``send_message`` / ``open_app`` / ``weather_report`` / ``run_capability`` are not inferred from bare
      JSON if the reply also contains conversational lines unless a line starts with
      an explicit ``tool_name(`` / ``tool_name {`` for that tool.
    """
    s = (content or "").strip()
    if not s or len(s) > 12_000:
        return []
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s).strip()

    def _one(name: str, args: dict) -> list[dict]:
        name, args = _coerce_computer_control_open_app_to_open_app(
            name, args if isinstance(args, dict) else {}
        )
        if name not in valid_names or not isinstance(args, dict):
            return []
        return [
            {
                "id": "from_model_text",
                "function": {
                    "name": name,
                    "arguments": args,
                },
            }
        ]

    def _from_parsed_tool_json(obj: object) -> list[dict]:
        """OpenAI-style ``{"name": "...", "arguments": {...}}`` (or a one-element list)."""
        if isinstance(obj, dict):
            fn_block = obj.get("function")
            if isinstance(fn_block, dict) and not isinstance(obj.get("name"), str):
                name = fn_block.get("name")
                raw_args = fn_block.get("arguments")
            else:
                name = obj.get("name") or obj.get("function") or obj.get("tool_name")
                if isinstance(name, dict):
                    name = name.get("name")
                raw_args = obj.get("arguments") or obj.get("parameters") or obj.get("args")
            if isinstance(name, str):
                if isinstance(raw_args, str):
                    args = parse_tool_arguments(raw_args)
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                if isinstance(args, dict) and (
                    args or name in _ALLOW_EMPTY_SYNTHETIC_JSON_ARGS
                ):
                    return _one(name, args)
        if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
            return _from_parsed_tool_json(obj[0])
        return []

    # JSON: whole message is a single tool object
    if s.lstrip().startswith("{"):
        try:
            obj_whole = json.loads(s)
        except json.JSONDecodeError:
            obj_whole = None
        got = _from_parsed_tool_json(obj_whole)
        if got and _allow_high_risk_synthetic_tool(s, got):
            return got

    _PAREN_TOOL = re.compile(
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*\([ \t]*(\{[\s\S]*\})[ \t]*\)[ \t]*$"
    )
    # Models often print ``web_search(query="...")`` as plain text (no JSON object).
    _WEB_SEARCH_KWARG = re.compile(
        r"^[ \t]*web_search[ \t]*\([ \t]*query[ \t]*[:=][ \t]*"
        r"(['\"])(.*?)\1[ \t]*,?\s*\)[ \t]*$",
        re.IGNORECASE | re.DOTALL,
    )
    # Some models wrap the pseudo-call in brackets and never emit native tool_calls.
    _WEB_SEARCH_KWARG_BRACKET = re.compile(
        r"^[ \t]*\[\s*web_search\s*\(\s*query\s*[:=]\s*"
        r"(['\"])(.*?)\1\s*,?\s*\)\s*\]\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    # TypeScript-style ``city:`` or Python ``city=`` (not JSON) inside parentheses.
    _WEATHER_CITY_KWARG = re.compile(
        r"^[ \t]*weather_report[ \t]*\([ \t]*city[ \t]*[:=][ \t]*"
        r"(['\"])(.*?)\1[ \t]*,?\s*\)[ \t]*$",
        re.IGNORECASE | re.DOTALL,
    )
    _WEATHER_EMPTY_PARENS = re.compile(
        r"^[ \t]*weather_report\s*\(\s*\)\s*$",
        re.IGNORECASE,
    )

    def _try_line(line: str) -> list[dict]:
        line = (line or "").strip()
        if not line:
            return []
        if line.lstrip().startswith("{"):
            try:
                obj_line = json.loads(line)
            except json.JSONDecodeError:
                obj_line = None
            got = _from_parsed_tool_json(obj_line)
            if got:
                return got
        m_p = _PAREN_TOOL.match(line)
        if m_p:
            name, json_blob = m_p.group(1), m_p.group(2)
            if name in valid_names:
                args = parse_tool_arguments(json_blob)
                if isinstance(args, dict) and (
                    args or name in _ALLOW_EMPTY_SYNTHETIC_JSON_ARGS
                ):
                    got = _one(name, args)
                    if got:
                        return got
        m_ws = _WEB_SEARCH_KWARG.match(line)
        if m_ws and "web_search" in valid_names:
            q = (m_ws.group(2) or "").strip()
            if q:
                got = _one("web_search", {"query": q})
                if got:
                    return got
        m_wsb = _WEB_SEARCH_KWARG_BRACKET.match(line)
        if m_wsb and "web_search" in valid_names:
            q = (m_wsb.group(2) or "").strip()
            if q:
                got = _one("web_search", {"query": q})
                if got:
                    return got
        m_wx = _WEATHER_CITY_KWARG.match(line)
        if m_wx and "weather_report" in valid_names:
            city = (m_wx.group(2) or "").strip()
            if city:
                got = _one("weather_report", {"city": city})
                if got:
                    return got
        if _WEATHER_EMPTY_PARENS.match(line) and "weather_report" in valid_names:
            got = _one("weather_report", {})
            if got:
                return got
        for name in sorted(valid_names, key=len, reverse=True):
            if not line.startswith(name):
                continue
            n = len(name)
            if n < len(line) and line[n] not in " \t\n({":
                continue
            rest = line[n:].lstrip()
            if not rest.startswith("{"):
                continue
            brace = line.find("{", n)
            blob = _balanced_json_slice(line, brace)
            if not blob:
                continue
            args = parse_tool_arguments(blob)
            if isinstance(args, dict) and (
                args or name in _ALLOW_EMPTY_SYNTHETIC_JSON_ARGS
            ):
                got = _one(name, args)
                if got:
                    return got
        return []

    candidates: list[str] = [s]
    for ln in s.splitlines():
        t = ln.strip()
        if t and t not in candidates:
            candidates.append(t)
    for cand in candidates:
        got = _try_line(cand)
        if got and _allow_high_risk_synthetic_tool(s, got):
            return got
    # Embedded JSON tool objects: only if the ``{`` is not buried after a long
    # same-line prose prefix (stops chit-chat + hallucinated ``open_app`` JSON).
    max_prefix = 96
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        prefix = _same_line_prefix_before_brace(s, i).strip()
        if len(prefix) > max_prefix:
            continue
        blob = _balanced_json_slice(s, i)
        if not blob or len(blob) < 12:
            continue
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        got = _from_parsed_tool_json(parsed)
        if got and _allow_high_risk_synthetic_tool(s, got):
            return got
    return []
