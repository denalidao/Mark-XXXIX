from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def main() -> None:
    args: dict = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_parse_error": raw[:500]}
    print(
        json.dumps(
            {"ok": True, "stub": True, "capability": HERE.name, "received_args": args},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
