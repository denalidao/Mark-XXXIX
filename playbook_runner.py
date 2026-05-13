"""
Run **capabilities** (markdown + scripts under ``capabilities/``) for new automation.

``ROUTER.json`` is the allowlist and metadata (browser profile hint, start URL). Most
folders ship a stub ``run.py`` that prints JSON until you replace it with real logic.
``youtube`` delegates to the existing ``youtube_video`` action for **play** / search.

**Local fragments:** a capability entry may use ``{"$ref": "relative/path.json"}``
(relative to the ``capabilities/`` directory). The referenced file must be a JSON
object; keys alongside ``$ref`` override loaded fields. Only filesystem paths are
resolved — no HTTP URLs. Resolved router data is cached in-process until
``ROUTER.json`` or any referenced file changes (mtime).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_router_cache_sig: str | None = None
_router_cache_data: dict[str, Any] | None = None


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def capabilities_root() -> Path:
    return _repo_root() / "capabilities"


def clear_router_cache() -> None:
    """Drop cached ``ROUTER.json`` merge (tests or after hot edits without mtime bump)."""
    global _router_cache_sig, _router_cache_data
    _router_cache_sig = None
    _router_cache_data = None


def _safe_ref_path(caps_root: Path, ref: str) -> Path:
    s = ref.strip().replace("\\", "/")
    if not s or s.startswith(("/", "\\")):
        raise ValueError(f"$ref must be relative to capabilities/: {ref!r}")
    if ".." in Path(s).parts:
        raise ValueError(f"unsafe $ref path: {ref!r}")
    p = (caps_root / s).resolve()
    root = caps_root.resolve()
    try:
        p.relative_to(root)
    except ValueError as e:
        raise ValueError(f"$ref escapes capabilities root: {ref!r}") from e
    return p


def _resolve_capability_entry(
    raw: dict[str, Any],
    caps_root: Path,
    *,
    stack: set[Path],
) -> dict[str, Any]:
    if "$ref" not in raw:
        return dict(raw)
    ref_s = raw["$ref"]
    if not isinstance(ref_s, str) or not ref_s.strip():
        raise ValueError("$ref must be a non-empty string")
    path = _safe_ref_path(caps_root, ref_s)
    if path in stack:
        raise ValueError(f"circular $ref chain involving {path}")
    stack.add(path)
    try:
        if not path.is_file():
            raise ValueError(f"$ref file not found: {ref_s!r} -> {path}")
        inner = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(inner, dict):
            raise ValueError(f"$ref target must be a JSON object: {ref_s!r}")
        merged: dict[str, Any] = {**inner}
        for k, v in raw.items():
            if k == "$ref":
                continue
            merged[k] = v
        if "$ref" in merged:
            return _resolve_capability_entry(merged, caps_root, stack=stack)
        return merged
    finally:
        stack.discard(path)


def _gather_ref_paths_from_router_doc(doc: dict[str, Any], caps_root: Path) -> list[Path]:
    out: list[Path] = []
    caps = doc.get("capabilities")
    if not isinstance(caps, dict):
        return out
    for entry in caps.values():
        if not isinstance(entry, dict):
            continue
        r = entry.get("$ref")
        if isinstance(r, str) and r.strip():
            try:
                out.append(_safe_ref_path(caps_root, r))
            except ValueError:
                continue
    return out


def _router_signature(router_path: Path, caps_root: Path) -> str:
    if not router_path.is_file():
        return "missing"
    doc = json.loads(router_path.read_text(encoding="utf-8"))
    sig_parts = [f"m={router_path.stat().st_mtime_ns}"]
    for p in sorted(
        _gather_ref_paths_from_router_doc(doc, caps_root),
        key=lambda x: str(x).lower(),
    ):
        if p.is_file():
            sig_parts.append(f"{p.as_posix()}:{p.stat().st_mtime_ns}")
        else:
            sig_parts.append(f"{p.as_posix()}:missing")
    return "|".join(sig_parts)


def _build_resolved_router(router_path: Path, caps_root: Path) -> dict[str, Any]:
    if not router_path.is_file():
        return {"capabilities": {}}
    raw = json.loads(router_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"capabilities": {}}
    caps = raw.get("capabilities")
    if not isinstance(caps, dict):
        caps = {}
    resolved: dict[str, Any] = {}
    for cid, entry in caps.items():
        if isinstance(entry, dict) and "$ref" in entry:
            resolved[cid] = _resolve_capability_entry(entry, caps_root, stack=set())
        elif isinstance(entry, dict):
            resolved[cid] = dict(entry)
        else:
            resolved[cid] = entry
    out = dict(raw)
    out["capabilities"] = resolved
    return out


def load_router(*, force_reload: bool = False) -> dict[str, Any]:
    global _router_cache_sig, _router_cache_data
    path = capabilities_root() / "ROUTER.json"
    caps_root = capabilities_root().resolve()
    sig = _router_signature(path, caps_root)
    if (
        not force_reload
        and _router_cache_sig == sig
        and _router_cache_data is not None
    ):
        return _router_cache_data
    data = _build_resolved_router(path, caps_root)
    _router_cache_sig = sig
    _router_cache_data = data
    return data


def get_router_capability_meta(capability_id: str) -> dict[str, Any]:
    """
    Return the merged capability block for ``capability_id`` from ``ROUTER.json``
    (after resolving any local ``$ref``). Raises ``ValueError`` if missing or not an object.
    """
    caps = load_router().get("capabilities") or {}
    meta = caps.get(capability_id)
    if not isinstance(meta, dict):
        raise ValueError(
            f"{capability_id!r} missing or invalid in capabilities/ROUTER.json"
        )
    return meta


def _resolve_capability_id(raw: str, caps: dict[str, Any]) -> str | None:
    r = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if r in caps:
        return r
    for k in caps:
        if str(k).lower() == r:
            return str(k)
    return None


def list_capability_ids() -> list[str]:
    caps = load_router().get("capabilities") or {}
    return sorted(caps.keys())


def run_capability(parameters: dict | None, *, player: Any = None) -> str:
    """
    Execute one capability by id. Returns a human-readable string for the model log.
    """
    params = dict(parameters or {})
    raw_id = (params.get("capability_id") or params.get("capability") or params.get("id") or "").strip()
    router = load_router()
    caps: dict[str, Any] = dict(router.get("capabilities") or {})
    cid = _resolve_capability_id(raw_id, caps)
    if not cid:
        known = ", ".join(sorted(caps.keys())) or "(empty router)"
        return f"Unknown **capability_id** {raw_id!r}. Known ids: {known}"

    meta = caps.get(cid) or {}
    delegate = (meta.get("delegate") or "").strip()

    if delegate == "youtube_video":
        from actions.youtube_video import youtube_video

        yt_params: dict[str, Any] = {
            "action": (params.get("action") or "play").strip().lower() or "play",
            "query": (params.get("query") or "").strip(),
        }
        for key in ("url", "save", "region"):
            if key in params and params[key] is not None:
                yt_params[key] = params[key]
        return youtube_video(parameters=yt_params, response=None, player=player) or "Done."

    run_py = capabilities_root() / cid / "run.py"
    if not run_py.is_file():
        browser = meta.get("browser", "")
        url = meta.get("start_url", "")
        return (
            f"Capability **{cid}** has no **run.py** yet — add one under "
            f"`capabilities/{cid}/`. Router hints: browser={browser!r} start_url={url!r}."
        )

    payload = {k: v for k, v in params.items() if k not in ("capability_id", "capability", "id")}
    try:
        proc = subprocess.run(
            [sys.executable, str(run_py)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(run_py.parent),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"Capability **{cid}** timed out after 180s."
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        tail = (err or out or "(no output)")[:800]
        return f"Capability **{cid}** exited {proc.returncode}: {tail}"
    if not out:
        return f"Capability **{cid}** finished with no stdout."
    return out
