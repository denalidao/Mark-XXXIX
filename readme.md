# 🤖 MARK XXXIX (39)
### The Ultimate Cross-Platform Personal AI Assistant — By FatihMakes

> 📺 **[Watch the full setup video on YouTube](https://youtu.be/ej1f5OE3SNQ?si=lCxDhJix9ungq1Ry)**

A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supporting Windows, macOS, and Linux. Local execution. Zero subscriptions. Engineered for total autonomy.

---

## ✨ Overview

MARK XXXIX represents the pinnacle of the Jarvis series, evolving into a more flexible and robust system. It bridges the gap between the operating system and human intent. Through natural dialogue, Mark 39 analyzes your screen, processes uploaded documents, and executes complex workflows with a brand-new, adaptive interface.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language (Gemini Live) or **local push-to-talk** via Whisper + Ollama |
| 🖥️ System Control | Launch apps, manage files, execute terminal commands |
| 🧩 Autonomous Tasks | High-level planning for complex, multi-step goals |
| 👁️ Visual Awareness | Real-time screen processing and webcam vision (Gemini Live or **local Ollama vision**) |
| 🧠 Persistent Memory | **`memory/long_term.json`** for durable facts (identity, prefs, projects) plus a **rolling session transcript** in **`memory/session_context.json`** (bounded excerpt in the system prompt — not full tool logs) |
| ⌨️ Hybrid Input | Type commands, upload files, or use **hold-to-talk (PTT)** in local Ollama mode |

---

## 🆕 What's New in XXXIX

- 📂 **Advanced File Handling** — New support for direct file uploads. Drop PDFs, source code, or images into the assistant to have them analyzed, summarized, or edited instantly.
- 🎨 **Adaptive & Flexible UI** — A complete overhaul of the interface. The new UI is fully resizable and responsive, featuring transparency controls and customizable layouts to fit your workspace perfectly.
- 🐧🍎 **Refined Cross-Platform Stability** — Major fixes for macOS and Linux compatibility. Core system actions are now more consistent across all three major operating systems.
- ⚡ **Optimized Core Engine** — Significant performance boost in tool-calling logic and response generation, resulting in a 40% faster interaction speed.
- 🦙 **Local Ollama path (enhanced)** — Run without a Gemini API key for **chat**: **Ollama `/api/chat`** for tools + chat, **faster-whisper** for PTT speech-to-text, **separate vision model** for screen/camera and `screen_find`. Spoken replies can use **Windows SAPI (`pyttsx3`)**, **Gemini neural TTS** (cloud; needs `gemini_api_key` for speech only), or **Coqui local TTS** (see below).
- 🔊 **VOICE OUTPUT (LOCAL)** — In Ollama mode the right panel picks **`tts_backend`**: **Windows (SAPI)**, **Gemini neural**, or **Coqui local**. Gemini: prebuilt voice names (Charon, Kore, …) → `gemini_live_voice`. **Coqui**: clone root + **registry model presets** (editable combo), optional **Gemini-after-Coqui** checkbox, settings saved to `config/api_keys.json`. **`mark_tts.py`** routes backends; **`mark_coqui_tts.py`** runs Coqui + **sounddevice**; Gemini path keeps **429 / quota** fallbacks to SAPI.
- 🐸 **Coqui / TechGym-TTS (local neural)** — Optional **low-latency** speech vs Gemini TTS (no per-utterance HTTP; GPU-friendly once PyTorch is CUDA-enabled). See **[Coqui local TTS (recommended for speed)](#coqui-local-tts-recommended-for-speed)** below.
- 🌐 **Web research upgrade** — `web_search` now supports:
  - `mode: "search"` for general snippets (default),
  - `mode: "news"` for headline-focused results,
  - `mode: "fetch"` with `url` for plain-text page extraction,
  - `mode: "compare"` for side-by-side comparisons.
- ⏰ **Reminder + cron controls (Windows-first)** — `reminder` now supports `action: "schedule" | "list" | "cancel"`, recurring schedules (`daily`, `weekly`, `weekdays` on Windows Task Scheduler), optional stable `job_name`, and optional `open_app_name` to launch/focus a desktop app after each reminder fires.
- 🗂️ **JSON session memory (local Ollama)** — After each reply, the host appends a short **user / assistant** pair to **`memory/session_context.json`** (gitignored). The next turn injects a **trimmed** “recent conversation” block into the system prompt so continuity does not depend on an ever-growing chat array or duplicate lines. Set **`MARK_CLEAR_SESSION_CONTEXT=1`** once before launch (or delete the file) to wipe session memory.
- 🧠 **Same-turn `run_capability` dedupe** — Repeated **parse_syntax_grammar** / **numbers_engine** calls on identical `text` in one user turn are skipped after the first real run, with a short tool message so the model stops looping.
- ⏱️ **Ollama `/api/chat` timeout** — Configurable read timeout via **`MARK_OLLAMA_CHAT_TIMEOUT`** (seconds) or **`ollama_chat_timeout_sec`** in `config/api_keys.json` (clamped), so hung chats fail fast instead of blocking for minutes.
- 🧩 **`capabilities/` + `ROUTER.json`** — Site and desktop playbooks invoked through **`run_capability`** (`capability_id` + args). See **[Capabilities router](#capabilities-router)** below.

---

## Capabilities router

Mark loads **`capabilities/ROUTER.json`**: an allowlist of **`capability_id`** values the model may pass to **`run_capability`**. Each entry picks a browser profile / start URL and may **`delegate`** to another tool (e.g. **`youtube`** → **`youtube_video`**).

| `capability_id` | Role |
|-----------------|------|
| **youtube** | YouTube in Edge; delegates search/play to **`youtube_video`**. |
| **github** | GitHub in Brave. |
| **x** | X (Twitter) in Edge. |
| **chatgpt** | ChatGPT in Edge. |
| **google_stitch** | Google Stitch in Edge. |
| **gmail** | Gmail in Edge. |
| **proton_mail** | Proton Mail in Edge with optional **CDP** automation path; see `capabilities/proton_mail/README.md`. |
| **vision_read** | Local-only vision: **health**, **read_screen**, **read_camera** (Ollama **`llava`** by default). |
| **telegram** | Desktop-first notes; optional web. |
| **whatsapp** | WhatsApp Web in Brave. |
| **denalidao** | DenaliDAO site in Brave. |
| **parse_syntax_grammar** | Desktop **`run.py`**: PSG-style **analyze** / **pipeline** / rules / affix tables on local text or paths. |
| **numbers_engine** | Desktop **`run.py`**: cipher / spec / **math_evaluate** / **monad** pipeline; loads **`data/derived/numbers_engine/quantum_cipher_engine_methods.json`**. |

More detail: **`capabilities/README.md`** and each subfolder’s **`README.md`**.

### Optional local corpus (parse / numbers)

**`data/corpus_config.json`** describes where large **text** and **PDF** libraries live (`data/text`, `data/pdf`). Those directories are **gitignored** so clones stay small. Copy your own materials locally if you use chunking or PDF alignment features.

---

## ⚡ Quick Start

```bash
git clone https://github.com/denalidao/Mark-XXXIX.git
cd Mark-XXXIX
pip install -r requirements.txt
playwright install
python main.py
```

> ⚠️ **Installation Note:** To keep the repository lightweight, some OS-specific dependencies are not bundled in `requirements.txt`. If you run into a `ModuleNotFoundError`, simply install the missing package via `pip install <module_name>` for your specific system.

> 💡 **Tip:** If you use a global Python environment that also has **TensorFlow**, `pip` may warn about **protobuf** versions. A **dedicated venv** for Mark (`python -m venv .venv` then activate and reinstall) avoids clashes.

---

## 📋 Requirements

| Requirement | Details |
|---|---|
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | **3.11+** for the app; **Coqui / TechGym-TTS** editable installs typically require **Python 3.11** (upstream constraint: 3.9–3.11). Use a **separate conda env** for Mark+Coqui if your global Python is 3.12 |
| **Microphone** | Required for Gemini Live; optional for **local Ollama** unless you use **PTT** (then needed for capture) |
| **API Key** | Optional if you use **local Ollama** instead of Gemini (see below) |
| **[Ollama](https://ollama.com)** | For local mode: daemon on **`http://127.0.0.1:11434`** by default; pull at least one **chat** model and one **vision** model (e.g. `llava`) for full screen features |

---

## 🦙 Local Ollama (no Gemini API key)

This fork adds a complete **local stack** alongside the original Gemini Live path.

### How it fits together

| Piece | Role |
|------|------|
| **Ollama chat model** (dropdown / `ollama_model`) | Reasoning, tool calls, replies via `POST /api/chat` |
| **Ollama vision model** (`MARK_OLLAMA_VISION_MODEL` or `ollama_vision_model`, default **`llava`**) | `screen_process`, webcam/screen questions, and **`screen_find`** — independent of the chat tag |
| **faster-whisper** (PTT) | Converts microphone audio to text; **first run** may download Whisper weights (separate from `ollama pull`) |
| **Voice output** (`tts_backend`) | **`pyttsx3`** — Windows SAPI (optional `tts_voice_substring` / `MARK_TTS_VOICE`). **`gemini`** — cloud Gemini TTS + prebuilt voice; **sounddevice** playback; subject to **latency and quota**. **`coqui`** — **local** Coqui / TechGym-TTS (**`mark_coqui_tts.py`**); **sounddevice** playback; **SAPI** still used if Coqui is not configured or fails. |
| **`mark_tts.py`** | Routes **Coqui → (optional Gemini) → SAPI** per settings; Gemini HTTP + audio normalization + quota logging. |
| **`mark_coqui_tts.py`** | Loads **`TTS`** (clone path or pip-installed), blocks **Tortoise** model ids, **CPU/GPU** clamping, **init-failure cache** (no repeated heavy load every utterance), diagnostics. |

**Audio I/O:** The project uses **`sounddevice`** (PortAudio), not PyAudio.

### Setup checklist

1. Start **`ollama serve`** (or the Ollama app).
2. **`ollama pull`** a chat model (e.g. **`qwen2.5:7b`**, `qwen2.5-coder:7b`) and a vision model (e.g. **`llava`**).
3. **`pip install -r requirements.txt`** and **`playwright install`**.
4. Run **`python main.py`** and choose **CONNECT LOCAL OLLAMA** in the setup overlay (or set `MARK_LLM_PROVIDER=ollama` and configure `config/api_keys.json`). **`config/api_keys.json` is gitignored** — do not commit your keys.

### UI in local mode

- **OLLAMA MODEL** — Populated from **`GET /api/tags`**. Changing it saves to `config/api_keys.json`. Use **↻** after `ollama pull`.
- **VOICE OUTPUT (LOCAL)** — Backend: **Windows (SAPI)** | **Gemini neural** | **Coqui local**. **Gemini neural** shows the **Gemini voice** dropdown (Charon, Kore, …). **Coqui** shows **clone root** + **model registry** (preset combo, still type custom ids) and **“If Coqui fails, try Gemini TTS”** (writes `coqui_failover_to_gemini`). Chat stays on **Ollama** regardless. **`MARK_TTS_BACKEND`** overrides `tts_backend` when set (see env table).
- **LOCAL VOICE (PTT)** — Appears when the Ollama backend is online: **hold** the button, speak, **release** to transcribe and send text to the **chat** model.

### Coqui local TTS (recommended for speed)

**Why use it:** Gemini TTS is **simple to set up** but pays **network + API latency** on every line and can hit **429 / quota** (then you hear SAPI). **Coqui** runs **on your machine**; after the model is cached, replies are typically **much faster** and work **offline** for speech (you still use Ollama for chat).

> ⚠️ **Important (active environment):** If you use CUDA with local Coqui TTS, Mark must be started from the **same conda environment** that has CUDA-enabled PyTorch + Coqui dependencies (for example `mark-coqui`).  
> Validate before launch:  
> `conda activate mark-coqui`  
> `python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`  
> If this prints `False`, Coqui will run CPU-only or fall back to SAPI depending on your setup.

**Requirements (Windows example):**

1. **Separate conda env** (e.g. `mark-coqui`) with **Python 3.11** if your default is 3.12 — TechGym / Coqui **editable** `pip install -e .` often rejects 3.12 in `setup.py`.
2. **Clone** [coqui-ai/TTS](https://github.com/coqui-ai/TTS) or your fork (e.g. **TechGym-TTS-scout**), then from that directory:  
   `pip install -e .`  
   (Use the **same env** as `python main.py`.)
3. **PyTorch with CUDA** in that env if you have an NVIDIA GPU — the **CPU-only** wheel (`torch … +cpu`) makes Coqui assert on `gpu=True`. Install from [PyTorch Get Started](https://pytorch.org/get-started/locally/) (e.g. `cu124` wheels) and verify:  
   `python -c "import torch; print(torch.cuda.is_available())"` → **`True`**.
4. **NumPy / OpenCV:** Coqui’s **`gruut`** expects **NumPy 1.x** (not 2.x); some **`opencv-python`** releases pull **NumPy 2.x**. If `pip` reports conflicts, pin e.g. **`numpy>=1.26,<2`** and **`opencv-python>=4.8,<4.12`** in that env.
5. **`config/api_keys.json`** (gitignored): set **`tts_backend`** to **`coqui`**, **`coqui_model_name`** (registry id, e.g. `tts_models/en/jenny/jenny` or `tts_models/en/ljspeech/tacotron2-DDC`), and optionally **`coqui_tts_repo_path`** (absolute path to clone root containing **`TTS/`**). If the path is empty, Mark imports **`TTS`** from the environment (after `pip install -e`).
6. **`coqui_use_cuda`:** Mark **turns GPU off automatically** if PyTorch reports no CUDA (avoids a hard assert). Set **`coqui_use_cuda`: false** explicitly on CPU-only machines.

**Optional cloud bridge:** With **`coqui_failover_to_gemini`: true** and a **`gemini_api_key`**, a Coqui miss can try **Gemini TTS** before SAPI — keys stay in the file; you choose the chain in the UI.

**After changing Coqui settings:** save in the UI (or restart) so the **Coqui engine cache** resets. If init failed once, Mark **does not** repeat a full heavy load every utterance until you fix config and reset.

### Hybrid voice: Ollama chat + Gemini TTS (optional flow)

1. Use **local Ollama** setup so the **OLLAMA MODEL** and voice panels are visible.
2. Add a **`gemini_api_key`** to `config/api_keys.json` (same key as full Gemini mode; file stays gitignored).
3. Under **VOICE OUTPUT (LOCAL)**, choose **Gemini neural (uses API key)** — sets **`tts_backend`** to **`gemini`**.
4. Pick a **Gemini voice** (e.g. Kore, Charon). That sets **`gemini_live_voice`**.
5. Restart **`python main.py`** after hand-editing JSON if you bypass the UI.

**Config keys (local speech):** `tts_backend` (`gemini` | `pyttsx3` | `coqui`), `gemini_live_voice`, optional `gemini_tts_model` (default **`gemini-2.5-flash-preview-tts`**, with automatic retry on 429 to **`gemini-3.1-flash-tts-preview`**; override with **`MARK_GEMINI_TTS_MODEL`**), optional `tts_voice_substring` for SAPI when on Windows mode. **Coqui:** `coqui_tts_repo_path`, `coqui_model_name`, optional `coqui_model_path` / `coqui_config_path`, `coqui_use_cuda`, `coqui_failover_to_gemini`, optional `coqui_speaker` / `coqui_language` for multi-speaker / multilingual models.

### Gemini TTS quotas and billing

Each spoken line uses the **Gemini API** (`models.generateContent` on a **TTS** model). **Free tier** limits are easy to hit (e.g. **429 RESOURCE_EXHAUSTED**); the app then **falls back to Windows SAPI** — often **Zira** if `tts_voice_substring` matches Zira — so it can *sound* like the UI failed when it is really **quota**. Watch the **terminal** for `[TTS]` lines. See [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) and [speech generation](https://ai.google.dev/gemini-api/docs/speech-generation).

### Ollama URL vs other proxies

Mark expects the **native Ollama HTTP API** at the configured base URL:

- **`GET {base}/api/tags`**
- **`POST {base}/api/chat`** with Ollama’s JSON body (and optional `images` on user messages for vision)

Default base is **`http://127.0.0.1:11434`**. A service on another port (e.g. **8082**) is **not** a port conflict with Ollama, but Mark will **only** work pointed at that host if it implements the **same** paths and payloads as Ollama (not only a health JSON at `/`). OpenAI-style gateways need different client code.

### Environment overrides

Optional; Aletheon-style names are supported where noted.

| Variable | Purpose |
|---|---|
| `MARK_LLM_PROVIDER` | `ollama` to force local mode, or `gemini` for cloud. |
| `MARK_OLLAMA_URL` | Ollama base URL (default `http://127.0.0.1:11434`). |
| `MARK_OLLAMA_CHAT_TIMEOUT` | Seconds for Ollama **`/api/chat`** read timeout (also **`ollama_chat_timeout_sec`** in JSON). |
| `MARK_CLEAR_SESSION_CONTEXT` | Set to **`1`** / **`true`** once before a run to wipe **`memory/session_context.json`** on the next user turn (then unset by the host). |
| `MARK_OLLAMA_MODEL` | Fixed chat model tag; overrides config and **disables** the UI dropdown when set. |
| `ALETHEON_LLM_ASSIST_OLLAMA_URL` | Same as `MARK_OLLAMA_URL` if the latter is unset. |
| `ALETHEON_LLM_ASSIST_OLLAMA_MODEL` | Same as `MARK_OLLAMA_MODEL` if the latter is unset. |
| `MARK_OLLAMA_VISION_MODEL` | Vision tag for screen/camera / `screen_find`; default **`llava`** if unset. |
| `MARK_DISABLE_TTS` | `1` / `true` to skip spoken replies in local mode. |
| `MARK_TTS_BACKEND` | `gemini`, `pyttsx3`, or `coqui` — **overrides** `tts_backend` in `api_keys.json` when set. |
| `MARK_COQUI_REPO` | Optional: overrides **`coqui_tts_repo_path`** when non-empty after trim. |
| `MARK_COQUI_MODEL_NAME` | Optional: overrides **`coqui_model_name`**. |
| `MARK_COQUI_CUDA` | `cpu` / `false` / `0` or `cuda` / `true` / `1` — overrides **`coqui_use_cuda`** when set. |
| `MARK_COQUI_FAILOVER_TO_GEMINI` | `true` / `false` — overrides **`coqui_failover_to_gemini`**. |
| `MARK_GEMINI_TTS_MODEL` | TTS model id for Gemini speech (overrides `gemini_tts_model` in JSON). |
| `MARK_GEMINI_VOICE` / `GEMINI_LIVE_VOICE` | Prebuilt Gemini voice name (overrides `gemini_live_voice` in JSON). |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Same keys as cloud Gemini; used for **Gemini TTS** in hybrid mode. |
| `MARK_WHISPER_SIZE` | Whisper size: `tiny` … `large-v3` (default `small`). |
| `MARK_WHISPER_DEVICE` | `cpu` or `cuda` for faster-whisper (default `cpu`). |
| `MARK_WHISPER_COMPUTE` | Override compute type (e.g. for GPU). |
| `MARK_WHISPER_LANGUAGE` | Whisper language code (default `en`). |

Gemini **Live** (mic streamed to the model, native audio) still requires the Gemini path. **Local mode** uses typed input, **PTT → Whisper → Ollama**, tools, and **Ollama vision** for screen tools as above.

---

## 🧩 Local stack: files touched (for contributors)

| Area | Files |
|------|--------|
| LLM routing / Ollama HTTP | `mark_llm_settings.py` |
| Local assistant loop + PTT queue | `jarvis_ollama.py`, `main.py` |
| Rolling session transcript (JSON) | `memory/session_context.py` |
| Tool runner (Ollama branches) | `jarvis_tool_runner.py` |
| Speech-to-text | `mark_voice.py` |
| Local TTS (Coqui + Gemini + SAPI) | `mark_tts.py`, `mark_coqui_tts.py` |
| UI (Ollama model, voice output, PTT) | `ui.py` |
| Screen vision + Gemini path | `actions/screen_processor.py` |
| `screen_find` + desktop tools | `actions/computer_control.py` |
| Other tools / planner using LLM config | `actions/code_helper.py`, `actions/dev_agent.py`, `actions/youtube_video.py`, `agent/planner.py`, `memory/config_manager.py` |
| Dependencies | `requirements.txt` (`faster-whisper`, etc.) |

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
|---|---|
| YouTube | [@FatihMakes](https://www.youtube.com/@FatihMakes) |
| Instagram | [@fatihmakes](https://www.instagram.com/fatihmakes) |
