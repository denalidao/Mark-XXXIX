import asyncio
import os
import re
import threading
import json
import sys
import shutil
import subprocess
import traceback
from pathlib import Path

# Windows: faster-whisper/ctranslate2 and NumPy/sounddevice may each link Intel
# OpenMP — duplicate libiomp5 triggers OMP #15 and can abort. Set before native imports.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import sounddevice as sd
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    assistant_persona_final_override,
    format_memory_for_prompt,
    load_memory,
)

from jarvis_tool_runner import run_jarvis_tool
from mark_llm_settings import get_gemini_live_voice_name, is_ollama_mode


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024


def _first_output_device_index_containing(substring: str) -> int | None:
    """First PortAudio host output whose name contains ``substring`` (case-insensitive)."""
    needle = (substring or "").strip().lower()
    if not needle:
        return None
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for i, d in enumerate(devices):
        try:
            if int(d.get("max_output_channels") or 0) < 1:
                continue
        except (TypeError, ValueError):
            continue
        name = (d.get("name") or "").lower()
        if needle in name:
            return int(i)
    return None


def _audio_prefs_from_api_keys() -> tuple[int | None, str | None, bool]:
    """Optional ``audio_output_device``, ``audio_output_name``, ``audio_prefer_bluetooth``."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None, None, False
    if not isinstance(data, dict):
        return None, None, False
    dev = data.get("audio_output_device")
    out_i: int | None = None
    if isinstance(dev, int):
        out_i = dev
    elif isinstance(dev, str) and dev.strip().isdigit():
        out_i = int(dev.strip())
    name = data.get("audio_output_name")
    out_n = name.strip() if isinstance(name, str) and name.strip() else None
    pref = data.get("audio_prefer_bluetooth")
    prefer_bt = pref in (True, 1, "1", "true", "yes", "on")
    return out_i, out_n, prefer_bt


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _gemini_playback_device():
    """
    PortAudio output for Gemini Live PCM (``sounddevice.RawOutputStream``).

    Resolution order:

    1. ``MARK_SD_OUTPUT_DEVICE`` / ``MARK_AUDIO_OUTPUT`` — integer index, or a
       non-numeric string passed through to sounddevice (host-specific).
    2. ``MARK_AUDIO_OUTPUT_NAME`` / ``MARK_SD_OUTPUT_NAME`` — substring matched
       against output device names (useful for **Bluetooth** speakers that are
       not Windows' default PortAudio device).
    3. ``config/api_keys.json``: ``audio_output_device`` (int),
       ``audio_output_name`` (substring), or ``audio_prefer_bluetooth`` (bool).
    4. ``MARK_AUDIO_PREFER_BLUETOOTH`` — pick first output whose name contains
       ``bluetooth`` (case-insensitive).

    **Local Ollama TTS** (``pyttsx3``) always follows **Windows' default playback
    device** in Settings → Sound; set your Bluetooth speaker as default there for
    voice replies, or use only the Gemini Live path for speaker routing via the
    options above.
    """
    raw = os.environ.get("MARK_SD_OUTPUT_DEVICE", os.environ.get("MARK_AUDIO_OUTPUT", "")).strip()
    if raw:
        try:
            idx = int(raw)
            print(f"[JARVIS] 🔊 Output pick: env index {idx}")
            return idx
        except ValueError:
            print(f"[JARVIS] 🔊 Output pick: env string selector {raw!r}")
            return raw

    for env_key in ("MARK_AUDIO_OUTPUT_NAME", "MARK_SD_OUTPUT_NAME"):
        sub = os.environ.get(env_key, "").strip()
        if not sub:
            continue
        idx = _first_output_device_index_containing(sub)
        if idx is not None:
            print(f"[JARVIS] 🔊 Output pick: {env_key} matched → index {idx}")
            return idx
        print(f"[JARVIS] 🔊 No output device matched {env_key}={sub!r}")

    cfg_i, cfg_n, cfg_bt = _audio_prefs_from_api_keys()
    if cfg_i is not None:
        print(f"[JARVIS] 🔊 Output pick: api_keys.json audio_output_device={cfg_i}")
        return cfg_i
    if cfg_n:
        idx = _first_output_device_index_containing(cfg_n)
        if idx is not None:
            print(f"[JARVIS] 🔊 Output pick: api_keys.json audio_output_name={cfg_n!r} → index {idx}")
            return idx
        print(f"[JARVIS] 🔊 api_keys audio_output_name={cfg_n!r} matched no device.")

    if cfg_bt or _truthy_env("MARK_AUDIO_PREFER_BLUETOOTH"):
        idx = _first_output_device_index_containing("bluetooth")
        if idx is not None:
            who = "api_keys audio_prefer_bluetooth" if cfg_bt else "MARK_AUDIO_PREFER_BLUETOOTH"
            print(f"[JARVIS] 🔊 Output pick: {who} → index {idx}")
            return idx
        print("[JARVIS] 🔊 Bluetooth preference set but no output device name contains 'bluetooth'.")

    print(
        "[JARVIS] 🔊 Output pick: PortAudio default. "
        "If Gemini voice is silent on a Bluetooth speaker, set "
        "audio_output_name to a substring of that device in config/api_keys.json "
        "(see device list: python -c \"import sounddevice as sd; print(sd.query_devices())\"), "
        "or set MARK_AUDIO_OUTPUT_NAME / MARK_AUDIO_PREFER_BLUETOOTH."
    )
    return None


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Web research: **search** (DuckDuckGo / Gemini), **news** (headlines), **compare**, "
            "or **fetch** (download one http(s) page as plain text for the assistant). "
            "Use **fetch** when the user gives a specific URL to read; use **search** for open topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Search query (required for search/news/compare).",
                },
                "mode": {
                    "type": "STRING",
                    "description": "search | news | fetch | compare (default: search).",
                },
                "url": {
                    "type": "STRING",
                    "description": "Full https URL — required when mode is **fetch**.",
                },
                "max_chars": {
                    "type": "INTEGER",
                    "description": "For fetch only: max characters of plain text (default 14000, max 40000).",
                },
                "items": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Items to compare (compare mode).",
                },
                "aspect": {"type": "STRING", "description": "price | specs | reviews (compare mode)."},
            },
            "required": [],
        },
    },
    {
        "name": "weather_report",
        "description": (
            "Gets the current weather (temperature, sky, wind, humidity) for a city using "
            "a live forecast API. If the user does not name a city, pass an empty string — "
            "the app uses ``weather_cities`` (array) or ``weather_city`` from api_keys.json."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {
                    "type": "STRING",
                    "description": (
                        "City and optional region/country, e.g. 'Seattle' or 'Paris, France'. "
                        "Use '' if the user did not specify a place."
                    ),
                },
                "time": {
                    "type": "STRING",
                    "description": "Ignored for now (current conditions only); may be 'today'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "send_message",
        "description": (
            "Sends a real message through a **named desktop app** (desktop automation: "
            "Win key / Mac ``open -a``). Use **only** when the user clearly asks to **send**, "
            "**email**, **text**, **DM**, or **message on [app]** to someone. Valid "
            "``platform`` values are the app name as installed, e.g. **Proton Mail**, **Gmail**, "
            "**WhatsApp**, **Telegram** — **must match what the user asked for**, never assume "
            "WhatsApp. Do **not** use for vague social phrases like \"say hi to my grandson\" "
            "or \"tell Mom hello\" — those are spoken in-character greetings, not messaging "
            "app actions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {
                    "type": "STRING",
                    "description": (
                        "Required. Desktop app to drive, exactly as the user said or as it "
                        "appears in the Start menu / Applications (e.g. Proton Mail, Gmail, "
                        "WhatsApp, Telegram)."
                    ),
                },
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": (
            "Schedules **desktop notifications** via **Windows Task Scheduler** (and one-shot "
            "equivalents on macOS/Linux). This is **not** the Windows **Clock → Alarm** app — "
            "alarms will **not** appear there. For \"tomorrow at 9am\" or any single time, use "
            "**recurrence: once** (default). Use **daily** / **weekly** / **weekdays** only when "
            "the user clearly wants a repeating schedule. Supports **list** / **cancel** of "
            "JARVIS* tasks and optional **open_app_name** after each notification (Windows)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "schedule (default) | list | cancel",
                },
                "date": {
                    "type": "STRING",
                    "description": (
                        "YYYY-MM-DD for the first fire — if the user said **tomorrow**, this must "
                        "be tomorrow's calendar date, not today."
                    ),
                },
                "time": {"type": "STRING", "description": "HH:MM 24h clock time for the trigger (schedule)"},
                "message": {"type": "STRING", "description": "Toast / notification text (schedule)"},
                "recurrence": {
                    "type": "STRING",
                    "description": (
                        "once (default for a single time like tomorrow 9am) | daily | weekly | "
                        "weekdays — **Windows only** for non-once. Omit or use **once** unless the "
                        "user asked for a repeating schedule."
                    ),
                },
                "job_name": {
                    "type": "STRING",
                    "description": "Short stable id for recurring jobs (becomes JARVISCron_<id>). Required for recurring if you need a predictable cancel name.",
                },
                "task_name": {
                    "type": "STRING",
                    "description": "Full Task Scheduler name from **action: list** (for cancel).",
                },
                "open_app_name": {
                    "type": "STRING",
                    "description": "Optional Windows app to **start** after the notification (same names as **open_app**, e.g. notepad, code, msedge).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the monitor or **webcam** and runs vision. You have NO eyes without this tool. "
            "Use ``angle: \"screen\"`` for what's on the **monitor** (windows, browser pixels). "
            "Use ``angle: \"camera\"`` for **physical** questions: what they **hold**, **in front of "
            "them**, **in my hand**, **on my desk** (object), or \"what do I have here\" toward "
            "the webcam — NOT for reading a web page (use browser_control get_text). "
            "Do NOT ask the user to describe the screen instead of calling this tool. "
            "After calling, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {
                    "type": "STRING",
                    "description": (
                        "'screen' = desktop capture; 'camera' = webcam for hands/objects "
                        "in front of the user. Default 'screen' — use 'camera' for holding / "
                        "in front of me / in my hand."
                    )
                },
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command. NEVER route to agent_task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks requiring multiple different tools. "
            "Examples: 'research X and save to file', 'find and organize files'. "
            "DO NOT use for single commands. NEVER use for Steam/Epic — use game_updater."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":     {"type": "STRING", "description": "Complete description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high (default: normal)"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use agent_task, browser_control, or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name (human user), assistant_name (what the AI calls itself), "
                        "age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]

class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._speaking_lock = threading.Lock()
        self.ui.on_text_command = self._on_text_command
        self._turn_done_event: asyncio.Event | None = None

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
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
        tail = assistant_persona_final_override(memory)
        if tail:
            parts.append(tail)

        voice_name = get_gemini_live_voice_name()
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
        )

    async def _execute_tool(
        self, fc, *, user_query: str | None = None
    ) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        out = await run_jarvis_tool(
            name,
            args,
            ui=self.ui,
            speak=self.speak,
            speak_error=self.speak_error,
            loop=asyncio.get_running_loop(),
            user_query=user_query,
        )
        payload: dict = {"result": out.get("result", "Done.")}
        if out.get("silent"):
            payload["silent"] = True
        return types.FunctionResponse(id=fc.id, name=name, response=payload)

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"Jarvis: {full_out}")
                            out_buf = []

                    if response.tool_call:
                        fn_responses = []
                        partial_user = " ".join(in_buf).strip()
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(
                                fc, user_query=partial_user or None
                            )
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")
        dev = _gemini_playback_device()
        try:
            q = dev if dev is not None else sd.default.device[1]
            info = sd.query_devices(q, "output")
            print(f"[JARVIS] 🔊 PCM output → {info.get('name', info)!r} (device={dev!r})")
        except Exception as e:
            print(f"[JARVIS] 🔊 Device query failed ({e}); using device={dev!r}")

        try:
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
        except Exception as e:
            print(f"[JARVIS] 🔊 Stream open failed device={dev!r} ({e}); retrying PortAudio default.")
            stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=None,
            )
        stream.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    async def run(self):
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        while True:
            try:
                print("[JARVIS] 🔌 Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)
                    self._turn_done_event = asyncio.Event()

                    print("[JARVIS] ✅ Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())

            except Exception as e:
                print(f"[JARVIS] ⚠️ {e}")
                traceback.print_exc()
            self.set_speaking(False)
            self.ui.set_state("THINKING")
            print("[JARVIS] 🔄 Reconnecting in 3s...")
            await asyncio.sleep(3)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        if is_ollama_mode():
            from jarvis_ollama import JarvisOllama

            jarvis = JarvisOllama(ui, TOOL_DECLARATIONS)
        else:
            jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


def _maybe_relaunch_for_coqui_env() -> None:
    """
    If Coqui TTS is selected but the active conda env is not ``mark-coqui``,
    relaunch this script under ``conda run -n mark-coqui``.

    Note: Python cannot reliably "activate" conda in-process; relaunch is robust.
    """
    try:
        from mark_llm_settings import get_local_tts_backend
    except Exception:
        return

    if get_local_tts_backend() != "coqui":
        return

    if os.environ.get("MARK_SKIP_COQUI_RELAUNCH", "").strip() == "1":
        return

    env_name = (os.environ.get("CONDA_DEFAULT_ENV") or "").strip()
    if env_name.lower() == "mark-coqui":
        # Helpful startup verification in the target env.
        try:
            import torch  # type: ignore[import-not-found]

            ok = bool(torch.cuda.is_available())
            print(f"[TTS] Coqui preflight: env=mark-coqui, torch={torch.__version__}, cuda={ok}")
            if not ok:
                print(
                    '[TTS] CUDA is false in mark-coqui. Verify with: '
                    'conda activate mark-coqui | python -c "import torch; print(torch.__version__, torch.cuda.is_available())"'
                )
        except Exception as ex:
            print(f"[TTS] Coqui preflight: could not import torch in mark-coqui ({ex}).")
        return

    conda_bin = shutil.which("conda")
    if not conda_bin:
        print(
            "[TTS] Coqui is enabled but conda is not on PATH; cannot auto-switch env. "
            'Run manually: conda activate mark-coqui | python -c "import torch; print(torch.__version__, torch.cuda.is_available())"'
        )
        return

    cmd = [conda_bin, "run", "-n", "mark-coqui", sys.executable, *sys.argv[1:]]
    child_env = os.environ.copy()
    child_env["MARK_SKIP_COQUI_RELAUNCH"] = "1"
    print(
        "[TTS] Coqui is enabled and current env is not mark-coqui. "
        "Relaunching under: conda run -n mark-coqui ..."
    )
    proc = subprocess.run(cmd, env=child_env)
    raise SystemExit(proc.returncode)

if __name__ == "__main__":
    _maybe_relaunch_for_coqui_env()
    main()