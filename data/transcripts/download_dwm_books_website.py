#!/usr/bin/env python3
"""
Mirror Internet Archive item DWM-books-website (David-Wynn Miller books & site bundle).

- Page images: Internet Archive BookReader renders each JP2 leaf as JPEG via
  BookReaderImages.php. BookReader URLs use page/n1 … page/n779 (see IA bookreader);
  those indices align 1:1 with JP2 filenames …_0001.jp2 … …_0779.jp2 for this item.
- Text: downloads bundled OCR / plain text derivatives (djvu.txt, page index, etc.).

Respect Archive.org bandwidth: default small delay between page requests.

Usage:
  python download_dwm_books_website.py
  python download_dwm_books_website.py --start 1 --end 20 --dry-run
  python download_dwm_books_website.py --scale 2 --skip-text

Source collection (metadata / bookreader):
  https://archive.org/details/DWM-books-website/mode/2up
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IDENTIFIER = "DWM-books-website"
METADATA_URL = f"https://archive.org/metadata/{IDENTIFIER}"
DOWNLOAD_BASE = f"https://archive.org/download/{IDENTIFIER}"
BOOKREADER_TEMPLATE = (
    f"https://archive.org/details/{IDENTIFIER}/page/n{{n}}/mode/2up"
)
USER_AGENT = "Mark-XXXIX-dwm-mirror/1.0 (local research; contact: none)"


def _http_get(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_download(url: str, dest: Path, timeout: float = 600.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)


def _find_jp2_zip_name(files: list[dict]) -> str:
    for f in files:
        name = f.get("name") or ""
        if name.lower().endswith("_jp2.zip"):
            return name
    raise RuntimeError("No *_jp2.zip in item metadata files[]")


def _jp2_folder_name(jp2_zip_name: str) -> str:
    """e.g. 'DWM Books & Website_jp2.zip' -> 'DWM Books & Website_jp2'."""
    if jp2_zip_name.lower().endswith(".zip"):
        return jp2_zip_name[:-4]
    return jp2_zip_name


def _book_stem_from_jp2_folder(folder: str) -> str:
    """e.g. 'DWM Books & Website_jp2' -> 'DWM Books & Website'."""
    if folder.endswith("_jp2"):
        return folder[: -len("_jp2")]
    return folder


def load_item_context() -> dict:
    raw = _http_get(METADATA_URL, timeout=60)
    meta = json.loads(raw.decode("utf-8"))
    files = meta.get("files") or []
    if not isinstance(files, list):
        raise RuntimeError("metadata files[] missing")
    jp2_zip = _find_jp2_zip_name(files)
    inner_prefix = _jp2_inner_prefix(jp2_zip)
    server = meta.get("server") or "ia800705.us.archive.org"
    item_dir = meta.get("dir") or f"/unknown/items/{IDENTIFIER}"
    return {
        "metadata": meta,
        "server": server,
        "item_dir": item_dir.rstrip("/"),
        "jp2_zip_name": jp2_zip,
        "inner_prefix": inner_prefix,
        "title": meta.get("metadata", {}).get("title") or IDENTIFIER,
    }


def bookreader_page_url(n: int) -> str:
    return BOOKREADER_TEMPLATE.format(n=n)


def bookreader_image_url(
    ctx: dict,
    leaf_index: int,
    scale: int,
) -> str:
    """JPEG tile for JP2 leaf (1-based leaf index matches …_0001.jp2)."""
    zip_path = f"{ctx['item_dir']}/{ctx['jp2_zip_name']}"
    inner = f"{ctx['inner_prefix']}/{ctx['inner_prefix'].split('/')[-1].replace('_jp2', '')}_{leaf_index:04d}.jp2"
    # inner file stem must match on-disk names inside the zip:
    # folder "DWM Books & Website_jp2" + file "DWM Books & Website_0001.jp2"
    folder = ctx["inner_prefix"]
    book_stem = folder[: -len("_jp2")] if folder.endswith("_jp2") else folder
    rel_file = f"{folder}/{book_stem}_{leaf_index:04d}.jp2"
    q = urllib.parse.urlencode(
        {
            "zip": zip_path,
            "file": rel_file,
            "id": IDENTIFIER,
            "scale": str(scale),
        },
        safe="/",
        quote_via=urllib.parse.quote,
    )
    return f"https://{ctx['server']}/BookReader/BookReaderImages.php?{q}"


def default_text_files() -> list[tuple[str, str]]:
    """(archive-relative name, local filename). Spaces must match IA file names."""
    return [
        ("DWM Books & Website_djvu.txt", "DWM Books & Website_djvu.txt"),
        ("DWM Books & Website_page_numbers.json", "DWM Books & Website_page_numbers.json"),
        ("DWM Books & Website_hocr_searchtext.txt.gz", "DWM Books & Website_hocr_searchtext.txt.gz"),
        ("DWM Books & Website_scandata.xml", "DWM Books & Website_scandata.xml"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Download IA item {IDENTIFIER} pages + text.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / IDENTIFIER,
        help="Output directory",
    )
    parser.add_argument("--start", type=int, default=1, help="First bookreader page index (inclusive)")
    parser.add_argument("--end", type=int, default=779, help="Last bookreader page index (inclusive)")
    parser.add_argument(
        "--scale",
        type=int,
        default=2,
        help="BookReaderImages scale (lower = smaller JPEG; IA accepts small integers)",
    )
    parser.add_argument("--delay", type=float, default=0.35, help="Seconds between page image requests")
    parser.add_argument("--dry-run", action="store_true", help="Print URLs only; do not write files")
    parser.add_argument("--skip-text", action="store_true", help="Do not download bundled text derivatives")
    parser.add_argument("--skip-images", action="store_true", help="Do not download page JPEGs")
    args = parser.parse_args()

    if args.start < 1 or args.end < args.start:
        print("Invalid --start/--end", file=sys.stderr)
        return 2)

    ctx = load_item_context()
    out: Path = args.out
    manifest_path = out / "manifest.jsonl"
    images_dir = out / "images"
    text_dir = out / "text"

    print("Item:", IDENTIFIER)
    print("Server:", ctx["server"])
    print("JP2 zip:", ctx["jp2_zip_name"])
    print("Output:", out)

    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    if not args.skip_text and not args.dry_run:
        text_dir.mkdir(parents=True, exist_ok=True)
        for remote, local_name in default_text_files():
            url = f"{DOWNLOAD_BASE}/{urllib.parse.quote(remote)}"
            dest = text_dir / local_name
            if dest.exists() and dest.stat().st_size > 0:
                print("skip existing text", local_name)
                continue
            print("text", local_name)
            try:
                _http_download(url, dest)
            except urllib.error.HTTPError as e:
                print("warn: could not download", remote, e.code, file=sys.stderr)

    if not args.skip_images:
        mode = "dry-run" if args.dry_run else "write"
        mf = None
        if not args.dry_run:
            mf = manifest_path.open("w", encoding="utf-8")

        for n in range(args.start, args.end + 1):
            img_url = bookreader_image_url(ctx, leaf_index=n, scale=args.scale)
            viewer_url = bookreader_page_url(n)
            dest = images_dir / f"page_n{n:04d}_scale{args.scale}.jpg"
            row = {
                "bookreader_index": n,
                "viewer_url": viewer_url,
                "image_url": img_url,
                "saved_to": str(dest.relative_to(out)) if not args.dry_run else None,
            }
            if args.dry_run:
                print(json.dumps(row))
                continue

            assert mf is not None
            if dest.exists() and dest.stat().st_size > 0:
                row["status"] = "skipped_exists"
                mf.write(json.dumps(row) + "\n")
                if n % 50 == 0 or n == args.start:
                    print("page", n, "exists")
                time.sleep(args.delay)
                continue

            try:
                _http_download(img_url, dest, timeout=180.0)
                row["status"] = "ok"
                row["bytes"] = dest.stat().st_size
            except Exception as e:
                row["status"] = "error"
                row["error"] = str(e)
                print("error page", n, e, file=sys.stderr)

            mf.write(json.dumps(row) + "\n")
            if n % 25 == 0 or n == args.start:
                print("page", n, row.get("status"), row.get("bytes"))

            time.sleep(args.delay)

        if mf is not None:
            mf.close()

    # Save metadata snapshot for reproducibility
    if not args.dry_run:
        snap = out / "ia_metadata_snapshot.json"
        if not snap.exists():
            snap.write_text(
                json.dumps(ctx["metadata"], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
