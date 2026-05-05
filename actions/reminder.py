import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_os() -> str:
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "windows").lower()
    except Exception:
        return "windows"


def _scripts_dir() -> Path:
    d = Path.home() / ".jarvis" / "reminders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitise(text: str, max_len: int = 200) -> str:
    return (
        text.replace("\\", "")
            .replace('"', "")
            .replace("'", "")
            .replace("\n", " ")
            .replace("\r", "")
            .strip()
    )[:max_len]


def _sanitize_task_token(raw: str, *, max_len: int = 36) -> str:
    t = re.sub(r"[^\w\-]+", "_", (raw or "").strip(), flags=re.ASCII)
    t = re.sub(r"_+", "_", t).strip("_")
    if not t:
        return ""
    return t[:max_len]


def _windows_trigger_block(target_dt: datetime, recurrence: str) -> str:
    """Task Scheduler 1.2 XML ``<Triggers>…</Triggers>`` (Windows)."""
    boundary = target_dt.strftime("%Y-%m-%dT%H:%M:%S")
    r = (recurrence or "once").lower().strip()
    if r == "daily":
        return (
            "<Triggers><CalendarTrigger>\n"
            f"    <StartBoundary>{boundary}</StartBoundary>\n"
            "    <Enabled>true</Enabled>\n"
            "    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
            "  </CalendarTrigger></Triggers>\n"
        )
    if r == "weekdays":
        return (
            "<Triggers><CalendarTrigger>\n"
            f"    <StartBoundary>{boundary}</StartBoundary>\n"
            "    <Enabled>true</Enabled>\n"
            "    <ScheduleByWeek>\n"
            "      <DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>\n"
            "      <WeeksInterval>1</WeeksInterval>\n"
            "    </ScheduleByWeek>\n"
            "  </CalendarTrigger></Triggers>\n"
        )
    if r == "weekly":
        tags = (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )
        day_tag = tags[target_dt.weekday()]
        return (
            "<Triggers><CalendarTrigger>\n"
            f"    <StartBoundary>{boundary}</StartBoundary>\n"
            "    <Enabled>true</Enabled>\n"
            "    <ScheduleByWeek>\n"
            f"      <DaysOfWeek><{day_tag}/></DaysOfWeek>\n"
            "      <WeeksInterval>1</WeeksInterval>\n"
            "    </ScheduleByWeek>\n"
            "  </CalendarTrigger></Triggers>\n"
        )
    # once — single calendar fire at boundary (same as original TimeTrigger semantics)
    return (
        "<Triggers><TimeTrigger>\n"
        f"    <StartBoundary>{boundary}</StartBoundary>\n"
        "    <Enabled>true</Enabled>\n"
        "  </TimeTrigger></Triggers>\n"
    )


def _list_windows_jarvis_tasks() -> str:
    result = subprocess.run(
        ["schtasks", "/Query", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return f"Could not list scheduled tasks: {err}"

    rows: list[str] = []
    reader = csv.reader(io.StringIO(result.stdout or ""))
    for parts in reader:
        if not parts:
            continue
        name = (parts[0] or "").strip().strip('"')
        if "\\" in name:
            name = name.split("\\")[-1].strip()
        if name.startswith("JARVISReminder_") or name.startswith("JARVISCron_"):
            nxt = (parts[1] or "").strip() if len(parts) > 1 else ""
            rows.append(f"- {name}  ({nxt})" if nxt else f"- {name}")
    if not rows:
        return "No J.A.R.V.I.S reminders or cron jobs are registered in Task Scheduler."
    return "Scheduled J.A.R.V.I.S jobs:\n" + "\n".join(rows)


def _cancel_windows_task(task_name: str) -> str:
    name = task_name.strip()
    if not name.startswith(("JARVISReminder_", "JARVISCron_")):
        return "Refusing to delete tasks outside the J.A.R.V.I.S namespace (name must start with JARVISReminder_ or JARVISCron_)."
    del_result = subprocess.run(
        ["schtasks", "/Delete", "/TN", name, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if del_result.returncode != 0:
        err = (del_result.stderr or del_result.stdout or "").strip()
        return f"Could not delete task {name!r}: {err}"
    script = _scripts_dir() / f"{name}.py"
    try:
        script.unlink(missing_ok=True)
    except OSError:
        pass
    return f"Deleted scheduled task {name!r}."


def _write_notify_script(
    task_name: str,
    message: str,
    os_name: str,
    *,
    self_delete: bool,
    open_app_normalized: str | None = None,
) -> Path:
    script_path = _scripts_dir() / f"{task_name}.py"
    msg_literal = json.dumps(message)  

    if os_name == "windows":
        notify_block = f"""
message = {msg_literal}
notified = False

try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass

if not notified:
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast("J.A.R.V.I.S Reminder", message, duration=15, threaded=False)
        notified = True
    except Exception:
        pass

if not notified:
    try:
        import subprocess
        subprocess.run(["msg", "*", "/TIME:30", message], check=False)
    except Exception:
        pass

try:
    import winsound
    for freq in [800, 1000, 1200]:
        winsound.Beep(freq, 180)
        import time; time.sleep(0.08)
except Exception:
    pass
"""

    elif os_name == "mac":
        notify_block = f"""
message = {msg_literal}
notified = False

try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass

if not notified:
    try:
        import subprocess
        script = 'display notification "{{}}" with title "J.A.R.V.I.S Reminder"'.format(
            message.replace('"', '')
        )
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass
"""

    else:  # linux
        notify_block = f"""
message = {msg_literal}
notified = False

try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass

if not notified:
    try:
        import subprocess
        subprocess.run(
            ["notify-send", "--urgency=normal", "--expire-time=15000",
             "J.A.R.V.I.S Reminder", message],
            check=False
        )
    except Exception:
        pass
"""

    win_open = ""
    if os_name == "windows" and (open_app_normalized or "").strip():
        app_lit = json.dumps((open_app_normalized or "").strip())
        win_open = f"""
# Optional: start / focus a Windows desktop app (``start`` protocol; same idea as open_app)
_open_target = {app_lit}
try:
    import subprocess
    import time as _time
    subprocess.Popen(["cmd", "/c", "start", "", _open_target], shell=False)
    _time.sleep(1.0)
except Exception:
    pass
"""

    delete_tail = ""
    if self_delete:
        delete_tail = """
# One-shot: remove this script after firing
try:
    pathlib.Path(__file__).unlink(missing_ok=True)
except Exception:
    pass
"""

    script_body = f"""# Auto-generated by J.A.R.V.I.S reminder — do not edit
import sys, os, pathlib
{notify_block}
{win_open}
{delete_tail}
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o600)   # owner read/write only
    return script_path

def _schedule_windows(
    target_dt: datetime,
    task_name: str,
    script_path: Path,
    message: str,
    *,
    recurrence: str = "once",
) -> str:
    python_exe = Path(sys.executable)
    pythonw = python_exe.parent / "pythonw.exe"
    if pythonw.exists():
        python_exe = pythonw

    xml_path = _scripts_dir() / f"{task_name}.xml"
    triggers = _windows_trigger_block(target_dt, recurrence)
    desc = "J.A.R.V.I.S cron (recurring)" if recurrence != "once" else "J.A.R.V.I.S one-shot reminder"
    xml_content = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f"  <RegistrationInfo><Description>{desc}</Description></RegistrationInfo>\n"
        f"  {triggers}"
        "  <Actions><Exec>\n"
        f"    <Command>{python_exe}</Command>\n"
        f'    <Arguments>"{script_path}"</Arguments>\n'
        "  </Exec></Actions>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        "  <Principals><Principal>\n"
        "    <LogonType>InteractiveToken</LogonType>\n"
        "    <RunLevel>LeastPrivilege</RunLevel>\n"
        "  </Principal></Principals>\n"
        "</Task>\n"
    )

    xml_path.write_text(xml_content, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
        capture_output=True, text=True,
    )

    try:
        xml_path.unlink(missing_ok=True)
    except Exception:
        pass

    if result.returncode != 0:
        script_path.unlink(missing_ok=True)
        err = (result.stderr or result.stdout).strip()
        print(f"[Reminder] ❌ schtasks: {err}")
        return ""  

    return task_name


def _schedule_mac(target_dt: datetime, task_name: str,
                  script_path: Path) -> str:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    label     = f"com.jarvis.reminder.{task_name}"
    plist_path = agents_dir / f"{label}.plist"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script_path}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Year</key>   <integer>{target_dt.year}</integer>
    <key>Month</key>  <integer>{target_dt.month}</integer>
    <key>Day</key>    <integer>{target_dt.day}</integer>
    <key>Hour</key>   <integer>{target_dt.hour}</integer>
    <key>Minute</key> <integer>{target_dt.minute}</integer>
  </dict>
  <key>RunAtLoad</key>         <false/>
  <key>StandardOutPath</key>   <string>/dev/null</string>
  <key>StandardErrorPath</key> <string>/dev/null</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    plist_path.chmod(0o644)

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        plist_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ launchctl: {result.stderr.strip()}")
        return ""

    return label


def _schedule_linux(target_dt: datetime, task_name: str,
                    script_path: Path) -> str:

    if shutil.which("systemd-run"):
        on_calendar = target_dt.strftime("%Y-%m-%d %H:%M:00")
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                f"--on-calendar={on_calendar}",
                f"--unit={task_name}",
                "--",
                sys.executable, str(script_path),
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return task_name
        print(f"[Reminder] ⚠️ systemd-run failed: {result.stderr.strip()}, trying 'at'")

    if shutil.which("at"):
        at_time = target_dt.strftime("%H:%M %Y-%m-%d")
        cmd_str = f"{sys.executable} {script_path}\n"
        result  = subprocess.run(
            ["at", at_time],
            input=cmd_str, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return task_name
        print(f"[Reminder] ❌ at: {result.stderr.strip()}")
        return ""

    print("[Reminder] ❌ Neither systemd-run nor at found on this Linux system.")
    return ""

def _resolve_open_app_for_script(raw: str | None, os_name: str) -> str | None:
    """Windows only: normalize like ``open_app`` for embedded ``start`` in the notify script."""
    if os_name != "windows" or not (raw or "").strip():
        return None
    from actions.open_app import _normalize

    return _normalize((raw or "").strip())


def reminder(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = (params.get("action") or "schedule").strip().lower()
    os_name = _get_os()

    if action not in ("schedule", "list", "cancel"):
        return f"Unknown **action** {action!r}. Use: schedule | list | cancel."

    if action == "list":
        if os_name == "windows":
            return _list_windows_jarvis_tasks()
        return (
            "Listing J.A.R.V.I.S scheduled jobs is only wired to **Windows Task Scheduler** "
            "right now."
        )

    if action == "cancel":
        raw = (params.get("task_name") or params.get("job_name") or "").strip()
        if not raw:
            return (
                "For **action: cancel**, pass **task_name** exactly as shown by **action: list**, "
                "or pass **job_name** (short id) to remove **JARVISCron_<job_name>**."
            )
        if os_name != "windows":
            return "Cancel is only implemented on Windows (Task Scheduler) for now."
        if raw.startswith(("JARVISReminder_", "JARVISCron_")):
            return _cancel_windows_task(raw)
        token = _sanitize_task_token(raw)
        if not token:
            return "Invalid **job_name** — use letters, numbers, dash, or underscore."
        return _cancel_windows_task(f"JARVISCron_{token}")

    # --- schedule (default) ---
    date_str = (params.get("date") or "").strip()
    time_str = (params.get("time") or "").strip()
    message = (params.get("message") or "Reminder").strip()
    recurrence = (params.get("recurrence") or "once").strip().lower()
    job_name_raw = (params.get("job_name") or "").strip()
    open_app_raw = (params.get("open_app_name") or "").strip()

    allowed_rec = {"once", "daily", "weekly", "weekdays"}
    if recurrence not in allowed_rec:
        return f"Unknown **recurrence** {recurrence!r}. Use: once | daily | weekly | weekdays."

    if recurrence != "once" and os_name != "windows":
        return (
            f"**recurrence: {recurrence}** is only supported on **Windows** right now "
            "(Task Scheduler calendar triggers). Use **once** on this OS, or run Mark on Windows."
        )

    if not date_str or not time_str:
        return "I need both **date** and **time** to schedule (YYYY-MM-DD and HH:MM)."

    try:
        target_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "I couldn't parse that date or time. Please use YYYY-MM-DD and HH:MM."

    if recurrence == "once" and target_dt <= datetime.now():
        return "That time has already passed — I can't set a one-shot reminder in the past."

    safe_msg = _sanitise(message)
    open_norm = _resolve_open_app_for_script(open_app_raw, os_name)

    if recurrence == "once":
        task_name = f"JARVISReminder_{target_dt.strftime('%Y%m%d_%H%M%S')}"
        self_delete = True
    else:
        token = _sanitize_task_token(job_name_raw) or target_dt.strftime("%Y%m%d_%H%M%S")
        task_name = f"JARVISCron_{token}"
        self_delete = False

    try:
        script_path = _write_notify_script(
            task_name,
            safe_msg,
            os_name,
            self_delete=self_delete,
            open_app_normalized=open_norm,
        )
    except Exception as e:
        return f"Could not prepare the reminder script: {e}"

    try:
        if os_name == "windows":
            job_id = _schedule_windows(
                target_dt, task_name, script_path, safe_msg, recurrence=recurrence
            )
        elif os_name == "mac":
            if recurrence != "once":
                script_path.unlink(missing_ok=True)
                return "Recurring schedules on macOS are not implemented yet — use **once**."
            job_id = _schedule_mac(target_dt, task_name, script_path)
        else:
            if recurrence != "once":
                script_path.unlink(missing_ok=True)
                return "Recurring schedules on Linux are not implemented yet — use **once**."
            job_id = _schedule_linux(target_dt, task_name, script_path)
    except Exception as e:
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ Scheduling exception: {e}")
        return "Something went wrong while scheduling the reminder."

    if not job_id:
        return "I couldn't register the reminder with the system scheduler."

    if player:
        player.write_log(
            f"[Reminder] ✅ {task_name} @ {date_str} {time_str} r={recurrence} — {safe_msg[:40]}"
        )

    friendly_time = target_dt.strftime("%B %d at %I:%M %p")
    if recurrence == "once":
        tail = f"One-shot reminder at {friendly_time} (task {task_name!r})."
    else:
        tail = (
            f"Recurring job **{task_name!r}** — {recurrence} at {time_str}, first run context "
            f"{friendly_time}. Use **action: list** to see it; **action: cancel** with "
            f"**task_name** or **job_name** to remove."
        )
    if open_norm:
        tail += f" Each run will try to start **{open_norm}** after the notification."
    if os_name == "windows":
        tail += (
            "\n\n**Note (Windows):** This is a **Task Scheduler** job plus a **desktop "
            "notification** (and optional beeps) — it does **not** add an entry to the "
            "**Clock → Alarm** app list. For a built-in Clock alarm with snooze in that UI, "
            "create it in **Clock** manually."
        )
    return tail