"""One-off: WebVTT -> deduped plain text (strip YouTube timing tags)."""
import re
from pathlib import Path

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def vtt_to_lines(vtt_path: Path) -> list[str]:
    """YouTube auto-captions repeat overlapping phrases; merge by prefix extension."""
    raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    segments: list[str] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("WEBVTT") or block.startswith("Kind:") or block.startswith("Language:"):
            continue
        parts = block.split("\n", 1)
        if len(parts) < 2:
            continue
        first, rest = parts[0], parts[1]
        if "-->" not in first:
            continue
        text = TAG_RE.sub("", rest.strip())
        text = WS_RE.sub(" ", text).strip()
        if not text:
            continue
        if not segments:
            segments.append(text)
            continue
        last = segments[-1]
        if text == last:
            continue
        if text.startswith(last):
            segments[-1] = text
            continue
        if last.startswith(text):
            continue
        segments.append(text)
    return segments


def main() -> None:
    vtt = Path(r"C:\Users\RacerX\AppData\Local\Temp\yt_transcript\J7-Rknkrnts.en.vtt")
    out_dir = Path(__file__).resolve().parent.parent / "data" / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_txt = out_dir / "J7-Rknkrnts_David_Wynn_Miller_Quantum_Grammar_FULL_SEMINAR.txt"
    out_meta = out_dir / "J7-Rknkrnts_SOURCE.txt"

    lines = vtt_to_lines(vtt)
    body = "\n".join(lines)
    header = (
        "Source: https://www.youtube.com/watch?v=J7-Rknkrnts\n"
        "Title: David Wynn Miller Quantum Grammar FULL SEMINAR - Complete Course Explained\n"
        "Transcript: English captions (WebVTT via yt-dlp); timing tags stripped; "
        "overlapping cues merged where one extends the previous by prefix.\n\n"
    )
    out_txt.write_text(header + body, encoding="utf-8")
    out_meta.write_text(
        "url=https://www.youtube.com/watch?v=J7-Rknkrnts\n"
        "video_id=J7-Rknkrnts\n"
        "title=David Wynn Miller Quantum Grammar FULL SEMINAR - Complete Course Explained\n"
        "duration_seconds=34262\n"
        f"transcript_lines={len(lines)}\n"
        f"transcript_chars={len(body)}\n"
        f"transcript_file={out_txt.name}\n",
        encoding="utf-8",
    )
    print(out_txt, len(lines), "lines", len(body), "chars")


if __name__ == "__main__":
    main()
