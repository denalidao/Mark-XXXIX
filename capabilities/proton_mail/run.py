"""Proton Mail playbook — stdin JSON → ``browser_control`` (Edge profile from ROUTER)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

_IS_WIN = platform.system() == "Windows"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CAP_DIR = Path(__file__).resolve().parent
_CAPABILITY_ID = _CAP_DIR.name


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

    return get_router_capability_meta(_CAPABILITY_ID)


def _require_str(meta: dict, key: str) -> str:
    v = meta.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(
            f"ROUTER.json → {_CAPABILITY_ID}.{key} must be a non-empty string."
        )
    return v.strip()


def _resolve_edge_exe(meta: dict) -> str:
    raw = (meta.get("edge_executable") or "").strip().strip('"')
    if raw and Path(raw).is_file():
        return raw
    w = shutil.which("msedge")
    if w and Path(w).is_file():
        return w
    if _IS_WIN:
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
            base = os.environ.get(env)
            if not base:
                continue
            cand = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if cand.is_file():
                return str(cand)
    return ""


def _open_mail_url_via_os_handlers(url: str) -> None:
    """Hand off HTTPS to Edge like Shell / URI activation (avoid extra automation-looking launch)."""
    u = (url or "").strip()
    if not u:
        return
    try:
        creation = 0
        if _IS_WIN and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        if _IS_WIN:
            edge_uri = u if u.lower().startswith("microsoft-edge:") else f"microsoft-edge:{u}"
            subprocess.run(
                ["cmd", "/c", "start", "", edge_uri],
                timeout=45,
                check=False,
                creationflags=creation,
            )
        elif platform.system() == "Darwin":
            subprocess.run(
                ["open", "-a", "Microsoft Edge", u],
                timeout=45,
                check=False,
            )
        else:
            import webbrowser

            webbrowser.open(u)
    except Exception:
        pass


def _maximize_foreground_window_windows() -> None:
    """Best-effort maximize of the currently focused window on Windows."""
    if not _IS_WIN:
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    except Exception:
        pass


def _spawn_edge_cdp_launch(url: str, exe: str, port: int, meta: dict) -> tuple[bool, str]:
    if not exe:
        return False, "Edge executable not found; set edge_executable in ROUTER.json."
    args: list[str] = [exe, f"--remote-debugging-port={port}"]
    if meta.get("cdp_spawn_disable_automation_controlled", True):
        args.append("--disable-blink-features=AutomationControlled")
    if meta.get("cdp_pass_start_url_on_command_line", False) and url.strip():
        args.append(url.strip())
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        return False, f"Could not start Edge: {e}"
    return True, ""


def _wait_for_all_edge_windows_closed(timeout_sec: float) -> bool:
    """Poll until no ``msedge.exe`` (profile released for the next CDP launch)."""
    if not _IS_WIN:
        return True
    deadline = time.time() + max(5.0, timeout_sec)
    while time.time() < deadline:
        if not _msedge_process_running_windows():
            time.sleep(0.55)
            if not _msedge_process_running_windows():
                return True
        else:
            time.sleep(0.75)
    return not _msedge_process_running_windows()


def _prelaunch_open_app_then_mail_url(meta: dict, inbox_url: str) -> tuple[bool, str]:
    """Same order you use manually: **open_app** (Start menu) then mailbox URL in Edge."""
    _ensure_repo_path()
    from actions.open_app import open_app

    app = (meta.get("prelaunch_app_name") or meta.get("browser") or "edge").strip()
    open_result = open_app(parameters={"app_name": app}, response=None, player=None) or ""
    open_ok = "opened" in open_result.lower()
    time.sleep(float(meta.get("prelaunch_open_app_wait_sec", 2.5)))
    _maximize_foreground_window_windows()
    u = inbox_url.strip()
    if u:
        _open_mail_url_via_os_handlers(u)
        time.sleep(0.35)
        _maximize_foreground_window_windows()
    time.sleep(float(meta.get("prelaunch_url_load_wait_sec", 4.0)))
    return open_ok, open_result


def _msedge_process_running_windows() -> bool:
    """True when at least one ``msedge.exe`` is running (profile dir likely locked)."""
    try:
        creation = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=creation,
        )
        out = (r.stdout or "").lower()
        return "msedge.exe" in out and "no tasks" not in out
    except Exception:
        return False


def _try_connect_cdp(
    browser_control: Callable[..., str],
    base: dict[str, Any],
    endpoint: str,
) -> tuple[bool, str]:
    attach = browser_control(
        {**base, "action": "connect_cdp", "cdp_url": endpoint},
        response=None,
        player=None,
    )
    return _browser_step_succeeded(attach), attach


def _bootstrap_edge_session(
    meta: dict,
    browser_control: Callable[..., str],
    base: dict[str, Any],
    initial_nav_url: str,
    navigation: str,
) -> tuple[bool, str]:
    """
    Try CDP; else wait for Edge to quit, optionally ``open_app`` + inbox URL, spawn Edge with
    ``--remote-debugging-port`` (inbox opened via OS / ``microsoft-edge:``, not as a trailing
    CLI URL), then CDP attach.
    """
    nav = navigation.strip().lower()
    if nav == "playwright_persistent":
        return True, ""
    if nav != "edge_then_cdp":
        return (
            False,
            f"Unknown navigation {navigation!r}; use edge_then_cdp or playwright_persistent.",
        )

    port = int(meta.get("cdp_port", 9222))
    wait_s = float(meta.get("cdp_connect_wait", 5.5))
    host = (meta.get("cdp_host") or "127.0.0.1").strip() or "127.0.0.1"
    endpoint = f"http://{host}:{port}"
    wait_user_close_s = float(meta.get("wait_user_close_edge_sec", 180))

    exe = _resolve_edge_exe(meta)

    # 1) Reuse CDP when Edge was started with --remote-debugging-port.
    ok_attach, attach = _try_connect_cdp(browser_control, base, endpoint)
    if ok_attach:
        return True, attach
    for _ in range(3):
        time.sleep(0.35)
        ok_attach, attach = _try_connect_cdp(browser_control, base, endpoint)
        if ok_attach:
            return True, attach

    # 2) Windows profile lock — wait until every Edge process exits (or timeout).
    if _IS_WIN and _msedge_process_running_windows():
        sys.stderr.write(
            "[proton_mail] Close EVERY Microsoft Edge window — automation will reopen Edge "
            f"with CDP port {port}. Waiting up to {wait_user_close_s:.0f}s…\n"
        )
        if not _wait_for_all_edge_windows_closed(wait_user_close_s):
            return (
                False,
                (
                    "Microsoft Edge stayed open beyond the timeout — close every window "
                    f"(no CDP responding at {endpoint}), then retry."
                ),
            )

    # 3) Optional ``open_app`` + mailbox URL (Start-menu ritual), then close-all again.
    if meta.get("prelaunch_open_app", False) and _IS_WIN:
        sys.stderr.write(
            "[proton_mail] prelaunch_open_app: open_app Edge, then inbox URL …\n"
        )
        _prelaunch_open_app_then_mail_url(meta, initial_nav_url)
        sys.stderr.write(
            "[proton_mail] Close ALL Edge windows — waiting up to "
            f"{wait_user_close_s:.0f}s …\n"
        )
        if not _wait_for_all_edge_windows_closed(wait_user_close_s):
            return (
                False,
                "Warm-up finished but Edge did not quit in time — close every window and retry.",
            )

    ok_sp, err_sp = _spawn_edge_cdp_launch(initial_nav_url, exe, port, meta)
    if not ok_sp:
        return False, err_sp
    time.sleep(wait_s)
    if not meta.get("cdp_pass_start_url_on_command_line", False) and initial_nav_url.strip():
        _open_mail_url_via_os_handlers(initial_nav_url.strip())
        time.sleep(0.35)
        _maximize_foreground_window_windows()
        time.sleep(float(meta.get("cdp_extra_wait_after_shell_url_sec", 2.0)))
    ok_attach, attach = _try_connect_cdp(browser_control, base, endpoint)
    if ok_attach:
        return True, attach
    for _ in range(4):
        time.sleep(1.2)
        ok_attach, attach = _try_connect_cdp(browser_control, base, endpoint)
        if ok_attach:
            return True, attach
    return (
        False,
        f"{attach} Quit all Edge instances and retry, or adjust cdp_connect_wait / cdp_port.",
    )


def _browser_step_succeeded(msg: str) -> bool:
    """False when ``browser_control`` reported a hard failure (not e.g. 'Could not find element')."""
    if not (msg or "").strip():
        return False
    m = msg.lower()
    if "browser error" in m:
        return False
    if "could not open" in m:
        return False
    if "could not start browser" in m:
        return False
    return True


def _emit(obj: dict, *, ok: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def _proton_open_compose_window(
    browser_control: Callable[..., str],
    base: dict[str, Any],
    start_url: str,
    compose_url: str,
    url_override: str,
) -> tuple[bool, str, str | None]:
    """
    Open Proton compose UI. Returns (success, detail, error_or_none).
    """
    if url_override:
        d = browser_control(
            {**base, "action": "go_to", "url": url_override},
            response=None,
            player=None,
        )
        if not _browser_step_succeeded(d):
            return False, d or "", "go_to failed (see detail)"
        return True, d or "", None
    if compose_url:
        d = browser_control(
            {**base, "action": "go_to", "url": compose_url},
            response=None,
            player=None,
        )
        if not _browser_step_succeeded(d):
            return False, d or "", "go_to compose_url failed (see detail)"
        return True, d or "", None
    r1 = browser_control(
        {**base, "action": "go_to", "url": start_url},
        response=None,
        player=None,
    )
    if not _browser_step_succeeded(r1):
        return False, r1 or "", "inbox navigation failed (see detail)"
    time.sleep(2.2)
    for label in ("New message", "Compose", "Nouveau message"):
        r2 = browser_control(
            {**base, "action": "smart_click", "description": label},
            response=None,
            player=None,
        )
        if r2 and "Could not find" not in r2 and _browser_step_succeeded(r2):
            return True, f"{r1} {r2}", None
    return (
        False,
        r1 or "",
        "Could not find compose control; set compose_url in ROUTER.json or pass url in stdin JSON.",
    )


def _proton_smart_type_first_match(
    browser_control: Callable[..., str],
    base: dict[str, Any],
    descriptions: tuple[str, ...],
    text: str,
) -> str:
    last = ""
    for desc in descriptions:
        last = browser_control(
            {
                **base,
                "action": "smart_type",
                "description": desc,
                "text": text,
            },
            response=None,
            player=None,
        ) or ""
        if last and "Could not find" not in last:
            return last
    return last


def _proton_fill_draft_and_send(
    browser_control: Callable[..., str],
    base: dict[str, Any],
    to_addr: str,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    parts: list[str] = []
    to_r = _proton_smart_type_first_match(
        browser_control,
        base,
        ("To", "Email address", "Recipients", "Recipient"),
        to_addr,
    )
    parts.append(to_r)
    if (
        "Could not find" in to_r
        or not to_r.strip()
        or not _browser_step_succeeded(to_r)
    ):
        return False, " ".join(parts) + " - could not fill To field."

    if subject.strip():
        sub_r = _proton_smart_type_first_match(
            browser_control,
            base,
            ("Subject", "Subject line"),
            subject.strip(),
        )
        parts.append(sub_r)
        if "Could not find" in sub_r or not _browser_step_succeeded(sub_r):
            return False, " ".join(parts) + " - could not fill Subject."

    body_r = _proton_smart_type_first_match(
        browser_control,
        base,
        ("Message", "Write your message", "Body", "message"),
        body,
    )
    parts.append(body_r)
    if "Could not find" in body_r or not _browser_step_succeeded(body_r):
        return False, " ".join(parts) + " - could not fill message body."

    send_r = browser_control(
        {**base, "action": "smart_click", "description": "Send"},
        response=None,
        player=None,
    )
    parts.append(send_r or "")
    if send_r and (
        "Could not find" in send_r or not _browser_step_succeeded(send_r)
    ):
        return False, " ".join(parts) + " - could not click Send (check Proton UI / confirmations)."
    return True, " ".join(parts)


def main() -> None:
    args = _read_stdin_json()
    if "_parse_error" in args:
        _emit(
            {
                "ok": False,
                "capability": _CAPABILITY_ID,
                "error": "stdin JSON parse failed",
                "detail": args.get("_parse_error"),
            },
            ok=False,
        )

    try:
        meta = _load_router_meta()
        browser = _require_str(meta, "browser")
        start_url = _require_str(meta, "start_url")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        _emit({"ok": False, "capability": _CAPABILITY_ID, "error": str(e)}, ok=False)

    compose_url = (meta.get("compose_url") or "").strip()
    action = (args.get("action") or "open").strip().lower() or "open"
    override = (args.get("url") or "").strip()
    dry_run = bool(args.get("dry_run"))

    if dry_run:
        would: dict[str, Any] = {
            "browser": browser,
            "start_url": start_url,
            "compose_url": compose_url or None,
            "url_override": override or None,
        }
        if action == "send":
            would["to"] = (args.get("to") or "")[:120]
            would["body_preview"] = (args.get("body") or "")[:80]
            would["subject"] = (args.get("subject") or "")[:80]
        if action == "read_screen":
            would["question"] = (args.get("question") or args.get("text") or "")[:300]
            would["skip_open"] = bool(args.get("skip_open"))
        would["navigation"] = meta.get("navigation") or "edge_then_cdp"
        would["prelaunch_open_app"] = bool(meta.get("prelaunch_open_app", False))
        would["wait_user_close_edge_sec"] = float(meta.get("wait_user_close_edge_sec", 180))
        would["cdp_pass_start_url_on_command_line"] = bool(
            meta.get("cdp_pass_start_url_on_command_line", False)
        )
        _emit(
            {
                "ok": True,
                "capability": _CAPABILITY_ID,
                "dry_run": True,
                "action": action,
                "would": would,
            },
            ok=True,
        )

    initial_nav_url = (override or start_url).strip()

    if action == "read_screen":
        _ensure_repo_path()
        from actions.screen_processor import analyze_screen_sync

        default_q = (meta.get("read_screen_default_question") or "").strip() or (
            "You are looking at Proton Mail in a browser. List visible inbox subject lines "
            "(top 10). If an email message is open, transcribe its subject and full visible "
            "body text. If you see a sign-in or login page, reply exactly: LOGIN_SCREEN"
        )
        question = (args.get("question") or args.get("text") or default_q).strip()
        settle = float(
            meta.get(
                "read_screen_settle_sec",
                args.get("settle_sec", 5.0),
            )
        )
        skip_open = bool(args.get("skip_open"))
        vision_timeout = int(meta.get("read_screen_vision_timeout_sec", 180))

        prelaunch_detail = ""
        opened_step_done = False
        if not skip_open:
            open_ok, open_msg = _prelaunch_open_app_then_mail_url(meta, initial_nav_url)
            prelaunch_detail = open_msg or ""
            # Windows open_app confirmation can be flaky. Do not fail-fast here:
            # URL handoff may still succeed, so continue and attempt screen analysis.
            if not open_ok:
                prelaunch_detail = (
                    f"{prelaunch_detail} [WARN] open_app not confirmed; continued with URL handoff."
                ).strip()
            opened_step_done = True
        time.sleep(max(0.5, settle))

        answer = analyze_screen_sync(question, speak=False, timeout=vision_timeout)
        if "LOGIN_SCREEN" in (answer or ""):
            _emit(
                {
                    "ok": False,
                    "capability": _CAPABILITY_ID,
                    "action": "read_screen",
                    "answer": answer,
                    "question": question,
                    "skip_open": skip_open,
                    "error": "LOGIN_SCREEN",
                    "hint": (
                        "Proton login is visible. Sign in manually in Edge, keep inbox visible, "
                        "then retry read_screen with skip_open=true."
                    ),
                    **({"prelaunch_detail": prelaunch_detail} if prelaunch_detail else {}),
                },
                ok=False,
            )

        ok = bool(answer) and not answer.startswith("[Vision]")
        if not ok and (answer or "").startswith("[Vision]"):
            _emit(
                {
                    "ok": False,
                    "capability": _CAPABILITY_ID,
                    "action": "read_screen",
                    "error": "VISION_FAILED",
                    "answer": answer,
                    "question": question,
                    "skip_open": skip_open,
                    "opened_step_done": opened_step_done,
                    **({"prelaunch_detail": prelaunch_detail} if prelaunch_detail else {}),
                    "hint": (
                        "Edge/URL open step ran. Reading failed in local vision backend. "
                        "Keep Proton inbox visible and retry with skip_open=true."
                    ),
                },
                ok=False,
            )
        _emit(
            {
                "ok": ok,
                "capability": _CAPABILITY_ID,
                "action": "read_screen",
                "answer": answer,
                "question": question,
                "skip_open": skip_open,
                "opened_step_done": opened_step_done,
                **({"prelaunch_detail": prelaunch_detail} if prelaunch_detail else {}),
                **({"error": answer} if not ok else {}),
            },
            ok=ok,
        )

    if action in ("open_native", "open_start"):
        open_ok, open_msg = _prelaunch_open_app_then_mail_url(meta, initial_nav_url)
        _emit(
            {
                "ok": True,
                "capability": _CAPABILITY_ID,
                "action": "open_native",
                "detail": open_msg or "",
                "opened_step_done": True,
                "open_app_confirmed": bool(open_ok),
                "target_url": initial_nav_url,
                **(
                    {
                        "warning": (
                            "open_app launch was not fully confirmed, but URL handoff was attempted."
                        )
                    }
                    if not open_ok
                    else {}
                ),
            },
            ok=True,
        )

    _ensure_repo_path()
    from actions.browser_control import browser_control

    base: dict = {"browser": browser}
    navigation = (meta.get("navigation") or "edge_then_cdp").strip()

    def _close_playwright() -> str:
        """Disconnect automation (CDP detach or close Playwright-held context). Leaves Edge.exe running."""
        try:
            return browser_control(
                {**base, "action": "close"},
                response=None,
                player=None,
            ) or ""
        except Exception as exc:  # noqa: BLE001
            return f"(close failed: {exc})"

    boot_ok, boot_detail = _bootstrap_edge_session(
        meta, browser_control, base, initial_nav_url, navigation
    )
    if not boot_ok:
        _emit(
            {
                "ok": False,
                "capability": _CAPABILITY_ID,
                "action": action,
                "error": boot_detail,
                "hint": (
                    "Follow any [proton_mail] stderr steps: close Edge when asked, wait for CDP relaunch. "
                    "With prelaunch_open_app, the playbook runs open_app (Edge) then the inbox URL before "
                    "restarting Edge with remote debugging. Keep ROUTER prelaunch_open_app / "
                    "wait_user_close_edge_sec as needed; or start Edge with --remote-debugging-port=<cdp_port> "
                    "and we attach only."
                ),
            },
            ok=False,
        )

    if action in ("open", "inbox"):
        target = override or start_url
        nav_l = navigation.strip().lower()
        if nav_l == "edge_then_cdp" and target.strip() == initial_nav_url:
            detail = f"Using live Edge (CDP); initial tab: {target}"
        else:
            detail = browser_control(
                {**base, "action": "go_to", "url": target},
                response=None,
                player=None,
            )
        closed = _close_playwright()
        nav_ok = _browser_step_succeeded(detail)
        _emit(
            {
                "ok": nav_ok,
                "capability": _CAPABILITY_ID,
                "action": action,
                "detail": f"{boot_detail} {detail}".strip(),
                "session_closed": closed,
                **(
                    {"error": "navigation failed (see detail)"}
                    if not nav_ok
                    else {}
                ),
            },
            ok=nav_ok,
        )

    if action == "compose":
        ok, det, err = _proton_open_compose_window(
            browser_control, base, start_url, compose_url, override
        )
        closed = _close_playwright()
        if ok:
            _emit(
                {
                    "ok": True,
                    "capability": _CAPABILITY_ID,
                    "action": "compose",
                    "detail": f"{boot_detail} {det}".strip(),
                    "session_closed": closed,
                },
                ok=True,
            )
        _emit(
            {
                "ok": False,
                "capability": _CAPABILITY_ID,
                "action": "compose",
                "detail": f"{boot_detail} {det}".strip(),
                "session_closed": closed,
                "error": err or "compose failed",
            },
            ok=False,
        )

    if action == "send":
        to_addr = (args.get("to") or "").strip()
        body = (args.get("body") or "").strip()
        subject = (args.get("subject") or "").strip()
        if not to_addr or not body:
            _emit(
                {
                    "ok": False,
                    "capability": _CAPABILITY_ID,
                    "action": "send",
                    "error": "send requires non-empty **to** and **body** in stdin JSON.",
                },
                ok=False,
            )
        ok_open, det_open, err_open = _proton_open_compose_window(
            browser_control, base, start_url, compose_url, override
        )
        if not ok_open:
            closed = _close_playwright()
            _emit(
                {
                    "ok": False,
                    "capability": _CAPABILITY_ID,
                    "action": "send",
                    "detail": f"{boot_detail} {det_open}".strip(),
                    "session_closed": closed,
                    "error": err_open or "could not open compose",
                },
                ok=False,
            )
        ok_fill, fill_detail = _proton_fill_draft_and_send(
            browser_control, base, to_addr, subject, body
        )
        closed = _close_playwright()
        full_detail = f"{boot_detail} {det_open} {fill_detail}".strip()
        if not ok_fill or not _browser_step_succeeded(full_detail):
            _emit(
                {
                    "ok": False,
                    "capability": _CAPABILITY_ID,
                    "action": "send",
                    "detail": full_detail,
                    "session_closed": closed,
                    "error": "send did not complete; see detail",
                },
                ok=False,
            )
        _emit(
            {
                "ok": True,
                "capability": _CAPABILITY_ID,
                "action": "send",
                "detail": full_detail,
                "session_closed": closed,
            },
            ok=True,
        )

    _emit(
        {
            "ok": False,
            "capability": _CAPABILITY_ID,
            "error": (
                "unknown action "
                f"{action!r}; use open_native/open_start, open, inbox, compose, send, or read_screen"
            ),
        },
        ok=False,
    )


if __name__ == "__main__":
    main()
