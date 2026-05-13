"""
Numbers / quantum cipher engine — stdin JSON → stdout JSON.

Loads ``data/derived/numbers_engine/quantum_cipher_engine_methods.json`` (spec)
and optionally the evidence index. Deterministic tagging and math helpers;
outputs follow ``recommended_output_schema`` where applicable.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SPEC_PATH = REPO / "data" / "derived" / "numbers_engine" / "quantum_cipher_engine_methods.json"
EVIDENCE_PATH = REPO / "data" / "derived" / "numbers_engine" / "numbers_engine_corpus_snippets.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _ordinal_map(spec: dict[str, Any]) -> dict[str, int]:
    lv = (spec.get("core_digit_tables") or {}).get("letter_values") or {}
    om = lv.get("ordinal_map") or {}
    out: dict[str, int] = {}
    for k, v in om.items():
        if isinstance(k, str) and len(k) == 1 and k.isalpha():
            try:
                out[k.upper()] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def _build_lexicon(spec: dict[str, Any]) -> dict[str, Any]:
    """Derive token→digit hints only from the bundled spec (no external word lists)."""
    roles = spec.get("syntax_digit_roles") or {}
    conj: set[str] = set()
    zero = roles.get("0")
    if isinstance(zero, dict):
        for ex in zero.get("examples") or []:
            if isinstance(ex, str) and ex.strip():
                conj.add(ex.strip().lower())
    time_rules = spec.get("time_and_position_cipher_rules") or {}
    invalid: dict[str, dict[str, Any]] = {}
    for row in time_rules.get("invalid_now_time_terms") or []:
        if not isinstance(row, dict):
            continue
        term = str(row.get("term") or "").strip().lower()
        if not term:
            continue
        invalid[term] = {
            "digit_or_role": row.get("digit_or_role"),
            "meaning": row.get("meaning"),
            "classification": row.get("classification"),
        }
    position_five = set()
    for t in time_rules.get("valid_now_time_position_terms") or []:
        if isinstance(t, str) and t.strip():
            position_five.add(t.strip().lower())
    return {"conjunctions_0": conj, "time_invalid": invalid, "position_preps_5": position_five}


def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+", text)


def _ordinal_sum(word: str, omap: dict[str, int]) -> int | None:
    letters = [c for c in word.upper() if c.isalpha()]
    if not letters or not all(c in omap for c in letters):
        return None
    return sum(omap[c] for c in letters)


def _homophone_hits(token_lower: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    hom = (spec.get("homophone_math_methods") or {}).get("rules") or []
    for rule in hom:
        if not isinstance(rule, dict):
            continue
        terms = [str(x).lower() for x in (rule.get("left_terms") or []) if str(x).strip()]
        res = str(rule.get("result") or "")
        if token_lower in terms or token_lower == res.split("/")[0].strip().lower():
            hits.append(
                {
                    "id": rule.get("id"),
                    "raw": rule.get("raw"),
                    "left_terms": rule.get("left_terms"),
                    "result": rule.get("result"),
                    "operation": rule.get("operation"),
                }
            )
    return hits


def _syntax_digit_for_token(
    token_lower: str, lex: dict[str, Any], spec: dict[str, Any]
) -> tuple[int | None, str | None, list[str]]:
    warnings: list[str] = []
    if token_lower in lex["conjunctions_0"]:
        return 0, "conjunction", warnings
    inv = lex["time_invalid"].get(token_lower)
    if inv:
        d = inv.get("digit_or_role")
        try:
            di = int(d) if d is not None else None
        except (TypeError, ValueError):
            di = None
        if di is not None and 0 <= di <= 9:
            cls = str(inv.get("classification") or "")
            return di, cls or "time_position_flag", warnings
    if token_lower in lex["position_preps_5"]:
        return 5, "position / preposition", warnings
    articles = {"the", "a", "an"}
    if token_lower in articles:
        return 6, "lodio / article", warnings
    aux_verbs = {"is", "are", "was", "were", "be", "been", "being", "am"}
    if token_lower in aux_verbs:
        return 2, "verb (auxiliary)", warnings
    return None, None, warnings


def _fact_phrase_spans(seq: list[int | None]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    i = 0
    while i + 2 < len(seq):
        if seq[i] == 5 and seq[i + 1] == 6 and seq[i + 2] == 7:
            spans.append({"start_index": i, "end_index": i + 2, "pattern": [5, 6, 7]})
            i += 3
        else:
            i += 1
    return spans


def _eval_syntax_first_order(tokens: list[Any]) -> float:
    """Apply multiply, divide, subtract, add passes in that order (spec ranks)."""
    t = [x for x in tokens]

    def pass_op(op: str) -> None:
        nonlocal t
        i = 0
        while i < len(t):
            if i > 0 and i + 1 < len(t) and t[i] == op and isinstance(t[i - 1], (int, float)) and isinstance(
                t[i + 1], (int, float)
            ):
                a, b = float(t[i - 1]), float(t[i + 1])
                if op == "x" or op == "*":
                    r = a * b
                elif op == "/":
                    r = a / b
                elif op == "-":
                    r = a - b
                elif op == "+":
                    r = a + b
                else:
                    i += 1
                    continue
                t = t[: i - 1] + [r] + t[i + 2 :]
                i = max(0, i - 1)
                continue
            i += 1

    for op in ("x", "*", "/", "-", "+"):
        pass_op(op)
    if len(t) == 1 and isinstance(t[0], (int, float)):
        return float(t[0])
    raise ValueError("syntax_first_order evaluation failed")


def _tokenize_math(expr: str) -> list[Any]:
    s = expr.replace("X", "x").replace("×", "x")
    parts = re.split(r"(\d+|[x*/+\-()])", s)
    out: list[Any] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.isdigit():
            out.append(int(p))
        elif p in "+-x*/()":
            out.append("*" if p == "x" else p)
        else:
            raise ValueError(f"unsupported token: {p!r}")
    return out


def _eval_standard(tokens: list[Any]) -> float:
    """Parentheses, then */ , then +-, left-to-right within precedence."""

    def parse_expr(i: int) -> tuple[float, int]:
        def parse_factor(idx: int) -> tuple[float, int]:
            if idx >= len(tokens):
                raise ValueError("unexpected end")
            tok = tokens[idx]
            if tok == "(":
                val, ni = parse_expr(idx + 1)
                if ni >= len(tokens) or tokens[ni] != ")":
                    raise ValueError("missing )")
                return val, ni + 1
            if isinstance(tok, (int, float)):
                return float(tok), idx + 1
            raise ValueError(f"expected number or '(' got {tok!r}")

        def parse_term(idx: int) -> tuple[float, int]:
            val, idx = parse_factor(idx)
            while idx < len(tokens) and tokens[idx] in ("*", "/"):
                op = tokens[idx]
                rhs, idx = parse_factor(idx + 1)
                val = val * rhs if op == "*" else val / rhs
            return val, idx

        val, idx = parse_term(i)
        while idx < len(tokens) and tokens[idx] in ("+", "-"):
            op = tokens[idx]
            rhs, idx = parse_term(idx + 1)
            val = val + rhs if op == "+" else val - rhs
        return val, idx

    v, pos = parse_expr(0)
    if pos != len(tokens):
        raise ValueError("trailing tokens")
    return v


def _evaluate_expression(expr: str, mode: str) -> dict[str, Any]:
    mode = (mode or "syntax_first_order").strip().lower()
    tokens = _tokenize_math(expr)
    if mode in ("syntax_first_order", "syntax", "first_order"):
        value = _eval_syntax_first_order(tokens)
    elif mode in ("standard", "standard_math_order", "pemdas"):
        value = _eval_standard(tokens)
    else:
        return {"ok": False, "error": f"unknown math mode: {mode!r}"}
    return {"ok": True, "mode": mode, "expression": expr, "value": value}


def _monad_profile(spec: dict[str, Any], numbers: list[float]) -> dict[str, Any]:
    mono = ((spec.get("word_value_methods") or {}).get("monad") or {}).get("engine_implementation") or {}
    if not numbers:
        return {
            "cipher_numbers": [],
            "sum": None,
            "factors": [],
            "whole_control_score": None,
            "monad_spec": mono,
        }
    s = sum(numbers)
    fac: list[int] = []
    n = int(abs(s)) if float(s).is_integer() else 0
    if n > 1:
        x = n
        d = 2
        while d * d <= x:
            while x % d == 0:
                fac.append(d)
                x //= d
            d += 1
        if x > 1:
            fac.append(x)
    return {
        "cipher_numbers": numbers,
        "sum": s,
        "factors": fac,
        "whole_control_score": float(s) if s == int(s) else s,
        "monad_spec": mono,
    }


def analyze_text(text: str, spec: dict[str, Any]) -> dict[str, Any]:
    omap = _ordinal_map(spec)
    lex = _build_lexicon(spec)
    raw_tokens = _tokenize_words(text)
    tokens_out: list[dict[str, Any]] = []
    seq: list[int | None] = []
    warnings: list[str] = []

    for w in raw_tokens:
        low = w.lower()
        digit, role, tw = _syntax_digit_for_token(low, lex, spec)
        warnings.extend(tw)
        norm = re.sub(r"[^A-Za-z0-9]", "", w).lower()
        osum = _ordinal_sum(w, omap) if w.isalpha() or "'" in w else None
        hom = _homophone_hits(low, spec)
        hg = hom[0]["id"] if hom else None

        entry = {
            "text": w,
            "normalized": norm,
            "syntax_digit": digit,
            "syntax_role": role,
            "letter_values": [omap.get(c, None) for c in w.upper() if c.isalpha()],
            "ordinal_sum": osum,
            "homophone_group": hg,
            "homophone_rules": hom,
            "prefix_flags": [],
            "suffix_flags": [],
        }
        tokens_out.append(entry)
        seq.append(digit)

    spans = _fact_phrase_spans(seq)
    nums_for_monad = [float(t["ordinal_sum"]) for t in tokens_out if t.get("ordinal_sum") is not None]
    monad = _monad_profile(spec, nums_for_monad)

    return {
        "input": text,
        "normalized_input": " ".join(raw_tokens),
        "tokens": tokens_out,
        "syntax_digit_sequence": seq,
        "fact_phrases": spans,
        "math_evaluations": [],
        "homophone_rewrites": [
            {"token": t["text"], "rules": t["homophone_rules"]}
            for t in tokens_out
            if t.get("homophone_rules")
        ],
        "monad_profile": monad,
        "classification": "quantum_cipher_engine_methods_v1",
        "warnings": warnings,
        "source_trace": [str(SPEC_PATH.relative_to(REPO))],
    }


def main() -> None:
    args: dict[str, Any] = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                print(json.dumps({"ok": False, "error": "invalid JSON on stdin"}))
                return

    action = (args.get("action") or "analyze").strip().lower()
    path = (args.get("document_path") or args.get("path") or "").strip()
    text = (args.get("text") or "").strip()

    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            print(json.dumps({"ok": False, "error": f"document_path not found: {path}"}))
            return
        text = p.read_text(encoding="utf-8", errors="replace")

    spec = _load_json(SPEC_PATH)
    if not spec:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"missing or empty spec: {SPEC_PATH}",
                }
            )
        )
        return

    if action == "spec":
        full = bool(args.get("full"))
        if full:
            print(json.dumps({"ok": True, "spec": spec}, ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "metadata": spec.get("metadata"),
                        "top_level_keys": sorted(spec.keys()),
                        "spec_path": str(SPEC_PATH),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return

    if action == "evidence":
        ev = _load_json(EVIDENCE_PATH)
        if not ev:
            print(json.dumps({"ok": False, "error": f"missing evidence: {EVIDENCE_PATH}"}))
            return
        section = (args.get("section_id") or "").strip()
        if section:
            block = (ev.get("evidence_by_section_id") or {}).get(section)
            print(json.dumps({"ok": True, "section_id": section, "evidence": block}, ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "version": ev.get("version"),
                        "section_notes": ev.get("section_notes"),
                        "section_ids": sorted((ev.get("evidence_by_section_id") or {}).keys()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return

    if action == "lexicon":
        lex = _build_lexicon(spec)
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "lexicon",
                    "engine_pipeline": spec.get("engine_pipeline"),
                    "math_operation_modes": spec.get("math_operation_modes"),
                    "recommended_output_schema": spec.get("recommended_output_schema"),
                    "derived_lexicon": {
                        k: sorted(v) if isinstance(v, set) else v for k, v in lex.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if action == "pipeline":
        if not text:
            print(json.dumps({"ok": False, "error": "pipeline requires **text** or **document_path**."}))
            return
        body = analyze_text(text, spec)
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "pipeline",
                    "engine_pipeline": spec.get("engine_pipeline"),
                    "analysis": body,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if action == "math_evaluate":
        expr = (args.get("expression") or args.get("expr") or text or "").strip()
        if not expr:
            print(json.dumps({"ok": False, "error": "math_evaluate requires **expression** or **text**."}))
            return
        mode = str(args.get("mode") or "syntax_first_order")
        try:
            out = _evaluate_expression(expr, mode)
            print(json.dumps(out, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e), "expression": expr}))
        return

    if action == "monad":
        nums = args.get("numbers")
        raw_nums: list[float] = []
        if isinstance(nums, list):
            for x in nums:
                if isinstance(x, (int, float)):
                    raw_nums.append(float(x))
                elif isinstance(x, str) and x.strip():
                    try:
                        raw_nums.append(float(x))
                    except ValueError:
                        continue
        profile = _monad_profile(spec, raw_nums)
        print(json.dumps({"ok": True, "action": "monad", "monad_profile": profile}, ensure_ascii=False, indent=2))
        return

    if action == "analyze":
        if not text:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "Missing **text** or **document_path**. Actions: analyze | pipeline | spec | "
                            "evidence | lexicon | math_evaluate | monad."
                        ),
                    }
                )
            )
            return
        out = analyze_text(text, spec)
        print(json.dumps({"ok": True, "action": "analyze", **out}, ensure_ascii=False, indent=2))
        return

    print(json.dumps({"ok": False, "error": f"unknown action: {action!r}"}))


if __name__ == "__main__":
    main()
