#!/usr/bin/env python3
"""
Build overlapping text chunks from Mark-XXXIX/data/text for RAG + rule review.

Reads data/corpus_config.json (paths relative to Mark-XXXIX repo root).
Optionally runs parse_syntax_grammar analyze per chunk (slow on large corpora).

Outputs:
  data/derived/quantum_grammar_library/manifest.json
  data/derived/quantum_grammar_library/chunks.jsonl

PDFs under data/pdf: manifest lists them; optional **pdf_alignment** adds
per-chunk `pdf_page_proportional` and (when PyMuPDF is installed) `pdf_page_anchor`
by scanning the PDF text layer once. Install: pip install pymupdf
"""

from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "data" / "corpus_config.json"


def _split_sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text.strip())
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    return [p.strip() for p in parts if p.strip()]


def _sentence_windows(
    sentences: list[str],
    max_n: int,
    overlap: int,
) -> Iterator[tuple[int, int, str]]:
    """Yields (start_idx, end_idx_exclusive, joined_text). end is exclusive."""
    if max_n < 1:
        raise ValueError("max_sentences_per_chunk must be >= 1")
    overlap = max(0, min(overlap, max_n - 1))
    step = max_n - overlap
    i = 0
    n = len(sentences)
    while i < n:
        j = min(i + max_n, n)
        body = " ".join(sentences[i:j])
        yield i, j, body
        if j >= n:
            break
        i += step


def _snap_end_to_newline(text: str, start: int, end: int, search_back: int) -> int:
    """If a newline appears in text[end-search_back:end], shrink end to after it."""
    lo = max(start + 1, end - search_back)
    chunk = text[lo:end]
    nl = chunk.rfind("\n")
    if nl == -1:
        return end
    return lo + nl + 1


def _char_windows(text: str, max_chars: int, overlap: int) -> Iterator[tuple[int, int, str]]:
    """Yields (char_start, char_end_exclusive, slice). Prefer breaking at newlines."""
    if max_chars < 500:
        raise ValueError("max_chars_per_chunk should be at least a few hundred")
    overlap = max(0, min(overlap, max_chars - 1))
    step = max_chars - overlap
    n = len(text)
    start = 0
    while start < n:
        raw_end = min(start + max_chars, n)
        end = _snap_end_to_newline(text, start, raw_end, min(2000, max_chars // 2))
        if end <= start + 200:
            end = raw_end
        body = text[start:end]
        yield start, end, body
        if end >= n:
            break
        start += step


def _load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("corpus_config must be a JSON object")
    return data


def _maybe_psg(chunk_text: str, run_py: Path) -> dict[str, Any] | None:
    payload = json.dumps({"action": "analyze", "text": chunk_text[:480_000]}, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, str(run_py)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "parse_syntax_grammar timeout"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:2000] or f"exit {proc.returncode}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON from parse_syntax_grammar"}


def _rel_repo(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _norm_for_anchor(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _pick_source(
    paths: list[Path],
    contains: str | None,
) -> Path | None:
    if not paths:
        return None
    if not contains:
        return paths[0]
    c = contains.lower()
    for p in paths:
        if c in p.name.lower():
            return p
    return None


def _build_pdf_alignment(
    txt_files: list[Path],
    pdf_files: list[Path],
    pa: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Open PDF once, build normalized text index for anchoring.
    Returns (aligner, manifest_fragment). aligner is None if unavailable.
    """
    meta: dict[str, Any] = {"enabled": bool(pa.get("enabled"))}
    if not pa.get("enabled"):
        meta["status"] = "off"
        return None, meta

    if importlib.util.find_spec("fitz") is None:
        meta["status"] = "skipped"
        meta["reason"] = "PyMuPDF not installed (pip install pymupdf)"
        return None, meta

    if not pdf_files:
        meta["status"] = "skipped"
        meta["reason"] = "no PDF files in library_pdf_dir"
        return None, meta

    t_sub = (pa.get("text_source_contains") or "").strip() or None
    p_sub = (pa.get("pdf_source_contains") or "").strip() or None
    txt_path = _pick_source(txt_files, t_sub)
    pdf_path = _pick_source(pdf_files, p_sub)
    if txt_path is None:
        meta["status"] = "skipped"
        meta["reason"] = "no matching .txt for text_source_contains"
        return None, meta
    if pdf_path is None:
        meta["status"] = "skipped"
        meta["reason"] = "no matching .pdf for pdf_source_contains"
        return None, meta

    import fitz  # type: ignore[import-not-found]

    doc = fitz.open(pdf_path)
    try:
        num_pages = doc.page_count
        between = "\n\n"
        norm_parts: list[str] = []
        for i in range(num_pages):
            page = doc.load_page(i)
            raw = page.get_text("text") or ""
            norm_parts.append(_norm_for_anchor(raw))
        norm_concat = between.join(norm_parts)
        cum_ends: list[int] = []
        pos = 0
        for i, n in enumerate(norm_parts):
            if i > 0:
                pos += len(between)
            pos += len(n)
            cum_ends.append(pos)
    finally:
        doc.close()

    snippet_n = max(32, min(512, int(pa.get("anchor_snippet_chars") or 96)))

    aligner: dict[str, Any] = {
        "txt_path": txt_path.resolve(),
        "pdf_path": pdf_path.resolve(),
        "pdf_relpath": _rel_repo(pdf_path),
        "num_pages": num_pages,
        "norm_concat": norm_concat,
        "cum_ends": cum_ends,
        "anchor_snippet_chars": snippet_n,
    }

    meta["status"] = "ok"
    meta["paired_text"] = _rel_repo(txt_path)
    meta["paired_pdf"] = _rel_repo(pdf_path)
    meta["pdf_num_pages"] = num_pages
    meta["norm_concat_chars"] = len(norm_concat)
    return aligner, meta


def _pdf_fields_for_chunk(
    aligner: dict[str, Any] | None,
    src: Path,
    full_text: str,
    char_start: int | None,
    body: str,
    *,
    sentence_index_start: int | None = None,
    sentence_total: int | None = None,
) -> dict[str, Any]:
    if aligner is None or src.resolve() != aligner["txt_path"].resolve():
        return {}
    n_pages = int(aligner["num_pages"])
    tlen = max(len(full_text), 1)

    if char_start is not None:
        prop = 1 + round((char_start / tlen) * max(n_pages - 1, 0))
    elif sentence_index_start is not None and sentence_total:
        st = max(int(sentence_total), 1)
        prop = 1 + round((sentence_index_start / st) * max(n_pages - 1, 0))
    else:
        prop = 1
    prop = max(1, min(n_pages, prop))

    out: dict[str, Any] = {
        "pdf_source_relpath": aligner["pdf_relpath"],
        "pdf_num_pages": n_pages,
        "pdf_page_proportional": prop,
    }

    nc = aligner.get("norm_concat") or ""
    ce = aligner.get("cum_ends") or []
    snip = aligner.get("anchor_snippet_chars", 96)
    if nc and ce and body.strip():
        needle = _norm_for_anchor(body[:snip])
        if len(needle) >= 24:
            sub = needle[: min(len(needle), 80)]
            idx = nc.find(sub)
            if idx != -1:
                ap = bisect.bisect_right(ce, idx) + 1
                ap = max(1, min(n_pages, ap))
                out["pdf_page_anchor"] = ap
                if abs(ap - prop) > 2:
                    out["pdf_page_alignment_note"] = "anchor_vs_proportional_mismatch"

    return out


def _pdf_alignment_dry_meta(
    pa: dict[str, Any],
    txt_files: list[Path],
    pdf_files: list[Path],
) -> dict[str, Any]:
    meta: dict[str, Any] = {"enabled": bool(pa.get("enabled")), "status": "dry_run"}
    if not pa.get("enabled"):
        meta["status"] = "off"
        return meta
    t_sub = (pa.get("text_source_contains") or "").strip() or None
    p_sub = (pa.get("pdf_source_contains") or "").strip() or None
    txt_path = _pick_source(txt_files, t_sub)
    pdf_path = _pick_source(pdf_files, p_sub)
    if txt_path:
        meta["would_pair_text"] = _rel_repo(txt_path)
    if pdf_path:
        meta["would_pair_pdf"] = _rel_repo(pdf_path)
    if importlib.util.find_spec("fitz") is None:
        meta["note"] = "PyMuPDF not installed; full run will skip pdf_alignment until: pip install pymupdf"
    else:
        meta["note"] = "PyMuPDF available; full run will open PDF once for alignment index"
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="Chunk library text for corpus + optional PSG tags.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to corpus_config.json")
    ap.add_argument("--force-psg", action="store_true", help="Override config attach_tags_per_chunk to true")
    ap.add_argument("--dry-run", action="store_true", help="Print counts only; do not write derived files")
    args = ap.parse_args()

    cfg = _load_config(args.config)
    text_dir = REPO_ROOT / str(cfg["library_text_dir"])
    pdf_dir = REPO_ROOT / str(cfg["library_pdf_dir"])
    derived = REPO_ROOT / str(cfg["derived_dir"])
    ch = cfg.get("chunking") or {}
    mode = str(ch.get("mode") or "chars").strip().lower()
    max_chars = int(ch.get("max_chars_per_chunk") or 14_000)
    overlap_c = int(ch.get("overlap_chars") or 1_200)
    max_sent = int(ch.get("max_sentences_per_chunk") or 80)
    overlap_s = int(ch.get("overlap_sentences") or 8)
    if mode not in ("chars", "sentences"):
        print(f"Unknown chunking mode: {mode!r} (use chars or sentences)", file=sys.stderr)
        return 2
    psg_cfg = cfg.get("parse_syntax_grammar") or {}
    run_rel = str(psg_cfg.get("relative_run_py") or "capabilities/parse_syntax_grammar/run.py")
    run_py = REPO_ROOT / run_rel
    do_psg = bool(psg_cfg.get("attach_tags_per_chunk")) or args.force_psg

    txt_files = sorted(text_dir.glob("*.txt"))
    pdf_files = sorted(pdf_dir.glob("*.pdf"))

    if not txt_files:
        print(f"No .txt under {text_dir}", file=sys.stderr)
        return 1

    manifest: dict[str, Any] = {
        "config_path": _rel_repo(args.config.resolve()),
        "library_text_dir": _rel_repo(text_dir),
        "library_pdf_dir": _rel_repo(pdf_dir),
        "text_sources": [_rel_repo(p) for p in txt_files],
        "pdf_sources": [_rel_repo(p) for p in pdf_files],
        "chunking": {
            "mode": mode,
            "max_chars_per_chunk": max_chars,
            "overlap_chars": overlap_c,
            "max_sentences_per_chunk": max_sent,
            "overlap_sentences": overlap_s,
        },
        "parse_syntax_grammar_per_chunk": do_psg,
        "parse_syntax_grammar_run_py": _rel_repo(run_py),
    }

    def _count_chunks_for_text(text: str) -> int:
        if mode == "sentences":
            sents = _split_sentences(text)
            return sum(1 for _ in _sentence_windows(sents, max_sent, overlap_s))
        return sum(1 for _ in _char_windows(text, max_chars, overlap_c))

    total_chunks = sum(_count_chunks_for_text(src.read_text(encoding="utf-8", errors="replace")) for src in txt_files)

    manifest["estimated_chunk_count"] = total_chunks

    pa = cfg.get("pdf_alignment") or {}
    if args.dry_run:
        manifest["pdf_alignment"] = _pdf_alignment_dry_meta(pa, txt_files, pdf_files)
        print(json.dumps(manifest, indent=2))
        print("dry-run: no files written")
        return 0

    aligner, pam = _build_pdf_alignment(txt_files, pdf_files, pa)
    manifest["pdf_alignment"] = pam

    if not run_py.is_file():
        print(f"Missing parse_syntax_grammar at {run_py}", file=sys.stderr)
        if do_psg:
            return 1

    derived.mkdir(parents=True, exist_ok=True)
    chunks_path = derived / "chunks.jsonl"
    manifest_path = derived / "manifest.json"

    chunk_seq = 0
    with chunks_path.open("w", encoding="utf-8") as out:
        for src in txt_files:
            text = src.read_text(encoding="utf-8", errors="replace")
            if mode == "sentences":
                sents = _split_sentences(text)
                windows: Iterator[tuple[int, int, str]] = _sentence_windows(sents, max_sent, overlap_s)
                for i, j, body in windows:
                    chunk_seq += 1
                    row = {
                        "chunk_id": f"{src.stem}:{chunk_seq:06d}",
                        "source_relpath": _rel_repo(src),
                        "chunking_mode": "sentences",
                        "sentence_index_start": i,
                        "sentence_index_end": j,
                        "char_start": None,
                        "char_end": None,
                        "char_len": len(body),
                        "text": body,
                    }
                    if do_psg:
                        row["parse_syntax_grammar"] = _maybe_psg(body, run_py)
                    row.update(
                        _pdf_fields_for_chunk(
                            aligner,
                            src,
                            text,
                            None,
                            body,
                            sentence_index_start=i,
                            sentence_total=len(sents),
                        )
                    )
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                for c0, c1, body in _char_windows(text, max_chars, overlap_c):
                    chunk_seq += 1
                    row = {
                        "chunk_id": f"{src.stem}:{chunk_seq:06d}",
                        "source_relpath": _rel_repo(src),
                        "chunking_mode": "chars",
                        "sentence_index_start": None,
                        "sentence_index_end": None,
                        "char_start": c0,
                        "char_end": c1,
                        "char_len": len(body),
                        "text": body,
                    }
                    if do_psg:
                        row["parse_syntax_grammar"] = _maybe_psg(body, run_py)
                    row.update(_pdf_fields_for_chunk(aligner, src, text, c0, body))
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest["written_chunks"] = chunk_seq
    manifest["chunks_relpath"] = _rel_repo(chunks_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(_rel_repo(manifest_path))
    print(_rel_repo(chunks_path), "lines=", chunk_seq)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
