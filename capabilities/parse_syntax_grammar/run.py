"""
Parse Syntax Grammar (PSG) — stdin JSON → stdout JSON.

Experimental symbolic layer aligned with transcript-derived rules in ``rules.json``.
Does not claim linguistic or legal truth; outputs are deterministic tags for RAG / diff.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "rules.json"
SUFFIXES_PATH = HERE / "suffixes.json"
PREFIXES_PATH = HERE / "prefixes.json"
ROOTS_PATH = HERE / "roots.json"
TRANSFORMATIONS_PATH = HERE / "transformations.json"
DICTIONARY_PATH = HERE / "dictionary.json"

# Heuristic POS (stdlib only; optional spaCy can be layered later).
_AUX = frozenset(
    "am is are was were be been being shall should will would can could may might must "
    "do does did have has had ought need dare".split()
)
_PREPS = frozenset(
    "in on at to for of with by from into through during before after above below "
    "between under out against among about without within throughout across onto "
    "upon off near toward towards".split()
)
_ARTICLES = frozenset({"a", "an", "the"})
_PRON = frozenset(
    "i you he she it we they me him her us them my your his her our their mine "
    "yours ours theirs myself yourself".split()
)


def _load_rules() -> dict:
    if not RULES_PATH.is_file():
        return {}
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _load_suffix_engine() -> dict:
    """
    Quantum Parse Syntax suffix module (``suffixes.json``).
    Longest suffix wins so e.g. ``ation`` matches before ``ion``.
    """
    if not SUFFIXES_PATH.is_file():
        return {
            "loaded": False,
            "metadata": {},
            "rule_groups": {},
            "suffixes_sorted": [],
        }
    data = json.loads(SUFFIXES_PATH.read_text(encoding="utf-8"))
    raw = data.get("suffixes") or []
    entries = [e for e in raw if isinstance(e, dict) and str(e.get("suffix", "")).strip()]
    entries.sort(key=lambda d: len(str(d.get("suffix", ""))), reverse=True)
    return {
        "loaded": True,
        "metadata": data.get("metadata") or {},
        "rule_groups": data.get("rule_groups") or {},
        "suffixes_sorted": entries,
    }


def _suffix_match(lemma: str, suffix_ctx: dict) -> dict | None:
    w = (lemma or "").lower()
    if len(w) < 2:
        return None
    for entry in suffix_ctx.get("suffixes_sorted") or []:
        suf = str(entry.get("suffix", "")).lower()
        if not suf or len(w) <= len(suf):
            continue
        if w.endswith(suf):
            return {
                "suffix": entry.get("suffix"),
                "class": list(entry.get("class") or []),
                "claimed_meaning": entry.get("claimed_meaning"),
                "examples": list(entry.get("examples") or []),
            }
    return None


def _load_prefix_engine() -> dict:
    """
    Quantum Parse Syntax prefix module (``prefixes.json``).
    Longest prefix wins at word start (e.g. ``un`` before ``u`` if both listed).
    """
    if not PREFIXES_PATH.is_file():
        return {
            "loaded": False,
            "metadata": {},
            "rule_groups": {},
            "prefixes_sorted": [],
        }
    data = json.loads(PREFIXES_PATH.read_text(encoding="utf-8"))
    raw = data.get("prefixes") or []
    entries = [e for e in raw if isinstance(e, dict) and str(e.get("prefix", "")).strip()]
    entries.sort(key=lambda d: len(str(d.get("prefix", ""))), reverse=True)
    return {
        "loaded": True,
        "metadata": data.get("metadata") or {},
        "rule_groups": data.get("rule_groups") or {},
        "prefixes_sorted": entries,
    }


def _prefix_match(lemma: str, prefix_ctx: dict) -> dict | None:
    w = (lemma or "").lower()
    if len(w) < 2:
        return None
    for entry in prefix_ctx.get("prefixes_sorted") or []:
        pre = str(entry.get("prefix", "")).lower()
        if not pre or len(w) <= len(pre):
            continue
        if w.startswith(pre):
            return {
                "prefix": entry.get("prefix"),
                "class": list(entry.get("class") or []),
                "claimed_meaning": entry.get("claimed_meaning"),
                "examples": list(entry.get("examples") or []),
            }
    return None


def _load_roots() -> dict:
    if not ROOTS_PATH.is_file():
        return {"loaded": False, "metadata": {}, "roots_sorted": []}
    data = json.loads(ROOTS_PATH.read_text(encoding="utf-8"))
    roots = [r for r in (data.get("roots") or []) if isinstance(r, dict) and str(r.get("root", "")).strip()]
    roots.sort(key=lambda d: len(str(d.get("root", ""))), reverse=True)
    return {
        "loaded": True,
        "metadata": data.get("metadata") or {},
        "roots_sorted": roots,
    }


def _load_transformations() -> dict:
    if not TRANSFORMATIONS_PATH.is_file():
        return {"loaded": False, "metadata": {}, "rules": []}
    data = json.loads(TRANSFORMATIONS_PATH.read_text(encoding="utf-8"))
    return {
        "loaded": True,
        "metadata": data.get("metadata") or {},
        "rules": list(data.get("rules") or []),
    }


def _load_dictionary() -> dict:
    if not DICTIONARY_PATH.is_file():
        return {"loaded": False, "metadata": {}, "by_word": {}, "count": 0}
    data = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    raw = data.get("entries") or data.get("words") or []
    by_word: dict[str, dict] = {}
    for e in raw:
        if not isinstance(e, dict):
            continue
        w = _norm_word(str(e.get("word", "")))
        if w:
            by_word[w] = e
    return {
        "loaded": True,
        "metadata": data.get("metadata") or {},
        "by_word": by_word,
        "count": len(by_word),
    }


def _flatten_claimed_meaning(cm: object) -> list[str]:
    if cm is None:
        return []
    if isinstance(cm, list):
        return [str(x) for x in cm]
    return [str(cm)]


def _affix_stem(lemma: str, pm: dict | None, sm: dict | None) -> str:
    """Strip matched prefix + suffix strings from lemma for root search."""
    w = (lemma or "").lower()
    pl = len(str((pm or {}).get("prefix") or ""))
    sl = len(str((sm or {}).get("suffix") or ""))
    if pl + sl >= len(w):
        return ""
    end = len(w) - sl if sl else len(w)
    return w[pl:end]


def _match_root_in_stem(stem: str, roots_sorted: list[dict]) -> dict | None:
    """Longest root listed first: first substring or full equality match wins."""
    if not stem:
        return None
    for r in roots_sorted:
        root = str(r.get("root", "")).lower()
        if not root or len(root) > len(stem):
            continue
        if stem == root or root in stem:
            return r
    return None


def _synthesize_token_meaning(t: dict, dict_row: dict | None) -> dict:
    if dict_row and dict_row.get("generated_meaning"):
        return {
            "generated_meaning": [str(x) for x in dict_row["generated_meaning"]],
            "source": "dictionary",
        }
    parts: list[str] = []
    pm = t.get("prefix_engine")
    if pm:
        parts.extend(_flatten_claimed_meaning(pm.get("claimed_meaning")))
    re = t.get("root_engine")
    if re and isinstance(re, dict):
        parts.extend(_flatten_claimed_meaning(re.get("claimed_meaning")))
    sm = t.get("suffix_engine")
    if sm:
        parts.extend(_flatten_claimed_meaning(sm.get("claimed_meaning")))
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return {"generated_meaning": out, "source": "composed" if out else "none"}


def _eval_transformation_rules(block: dict, trans_ctx: dict) -> list[dict]:
    if not trans_ctx.get("loaded"):
        return []
    tokens = block.get("tokens") or []
    vcount = int(block.get("verb_count") or 0)
    max_v = int(block.get("verb_allowed") or 1)
    fact = bool(block.get("fact_anchor"))
    any_vowel = any(t.get("begins_with_vowel") for t in tokens)
    any_neg_pre = any(t.get("negation_prefix_claim") for t in tokens)
    triggered: list[dict] = []
    for rule in trans_ctx.get("rules") or []:
        cond = str(rule.get("condition") or "").strip()
        eff = rule.get("effect")
        ok = False
        if cond == "word_starts_with_vowel":
            ok = any_vowel
        elif cond == "multiple_verbs":
            ok = vcount > max_v
        elif cond == "missing_preposition_anchor":
            ok = not fact
        elif cond == "negation_prefix_any":
            ok = any_neg_pre
        if ok:
            triggered.append({"condition": cond, "effect": eff})
    return triggered


def _enrich_decomposition(
    t: dict,
    roots_ctx: dict,
    dict_ctx: dict,
) -> None:
    lemma = t.get("lemma") or ""
    pm = t.get("prefix_engine")
    sm = t.get("suffix_engine")
    dict_row = (dict_ctx.get("by_word") or {}).get(lemma.lower())

    if dict_row:
        t["decomposition"] = {
            "prefix": dict_row.get("prefix"),
            "root": dict_row.get("root"),
            "suffix": dict_row.get("suffix"),
            "stem": dict_row.get("root"),
            "source": "dictionary",
        }
        stem = str(dict_row.get("root") or "").lower()
    else:
        stem = _affix_stem(lemma, pm, sm)
        t["decomposition"] = {
            "prefix": (pm or {}).get("prefix"),
            "suffix": (sm or {}).get("suffix"),
            "stem": stem,
            "source": "detected",
        }

    root_hit = _match_root_in_stem(stem, roots_ctx.get("roots_sorted") or [])
    if root_hit:
        t["root_engine"] = {
            "root": root_hit.get("root"),
            "claimed_meaning": root_hit.get("claimed_meaning"),
            "source": "roots.json",
        }
    else:
        t["root_engine"] = None

    t["meaning_synthesis"] = _synthesize_token_meaning(t, dict_row)


def _norm_word(raw: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", raw, flags=re.UNICODE).lower()


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p for p in parts if p.strip()]


def _tokenize(sentence: str) -> list[str]:
    return [t for t in re.findall(r"\S+", sentence) if t]


def classify_token(word: str, rules: dict) -> dict:
    w = _norm_word(word)
    if not w:
        return {"surface": word, "lemma": "", "pos_guess": "punct"}
    if w in _PREPS:
        return {"surface": word, "lemma": w, "pos_guess": "preposition"}
    if w in _ARTICLES:
        return {"surface": word, "lemma": w, "pos_guess": "article"}
    if w in _PRON:
        return {"surface": word, "lemma": w, "pos_guess": "pronoun"}
    if w.endswith("ly") and len(w) > 3:
        return {"surface": word, "lemma": w, "pos_guess": "adverb"}
    if w in _AUX:
        return {"surface": word, "lemma": w, "pos_guess": "verb", "verb_kind": "auxiliary"}
    if w.endswith("ing") or w.endswith("ed") or w.endswith("es") or w.endswith("s"):
        if w not in _PREPS and len(w) > 2:
            return {"surface": word, "lemma": w, "pos_guess": "verb", "verb_kind": "lexical"}
    return {"surface": word, "lemma": w, "pos_guess": "noun_or_other"}


def count_verbs(tokens: list[dict]) -> int:
    n = 0
    for t in tokens:
        if t.get("pos_guess") == "verb":
            n += 1
    return n


def _has_fact_anchor_pattern(tokens: list[dict]) -> bool:
    """Loose scan: prep ... (article|pronoun possessive) ... noun_or_other."""
    for i, t in enumerate(tokens):
        if t.get("pos_guess") != "preposition":
            continue
        window = tokens[i + 1 : i + 6]
        has_art = any(x.get("pos_guess") == "article" for x in window)
        has_n = any(x.get("pos_guess") == "noun_or_other" for x in window)
        if has_art and has_n:
            return True
        if t.get("lemma") in ("with", "for") and any(
            x.get("pos_guess") == "pronoun" for x in window[:3]
        ):
            has_n2 = any(x.get("pos_guess") == "noun_or_other" for x in window)
            if has_n2:
                return True
    return False


def vowel_claim_flags(word: str, rules: dict) -> dict:
    w = _norm_word(word)
    vowels = tuple((rules.get("vowel_prefix_negation") or {}).get("vowels") or "aeiou")
    sfx = tuple((rules.get("contract_suffixes") or []))
    out: dict = {"begins_with_vowel": bool(w[:1] in vowels), "contract_suffix": None}
    for s in sfx:
        if w.endswith(s) and len(w) > len(s) + 1:
            out["contract_suffix"] = s
            break
    neg_prefixes = tuple((rules.get("negative_prefixes") or []))
    out["negative_prefix_match"] = next((p for p in neg_prefixes if w.startswith(p)), None)
    if (rules.get("vowel_prefix_negation") or {}).get("enabled") and out["begins_with_vowel"]:
        out["claimed_classification_hint"] = "non_contractual_vowel_claim"
    return out


def classify_sentence(
    sentence: str,
    rules: dict,
    suffix_ctx: dict,
    prefix_ctx: dict,
    roots_ctx: dict,
    trans_ctx: dict,
    dict_ctx: dict,
) -> dict:
    raw_tokens = _tokenize(sentence)
    tokens = [classify_token(tok, rules) for tok in raw_tokens]
    for i, t in enumerate(tokens):
        t.update(vowel_claim_flags(t["surface"], rules))
        sm = _suffix_match(t.get("lemma") or "", suffix_ctx)
        t["suffix_engine"] = sm
        if sm and "contract_state" in (sm.get("class") or []):
            t["contract_state_suffix"] = True
        pm = _prefix_match(t.get("lemma") or "", prefix_ctx)
        t["prefix_engine"] = pm
        if pm and "negation" in (pm.get("class") or []):
            t["negation_prefix_claim"] = True
        _enrich_decomposition(t, roots_ctx, dict_ctx)
    vcount = count_verbs(tokens)
    max_v = int((rules.get("sentence_rules") or {}).get("max_primary_verbs") or 1)
    adverb_hits = sum(1 for t in tokens if t.get("pos_guess") == "adverb")
    fact_anchor = _has_fact_anchor_pattern(tokens)

    if vcount > max_v:
        cls = "fictional"
    elif adverb_hits >= 2 or (adverb_hits >= 1 and not fact_anchor):
        cls = "motional"
    elif vcount <= max_v and fact_anchor:
        cls = "factual"
    else:
        cls = "quantum_parse_candidate"

    block = {
        "text": sentence.strip(),
        "tokens": tokens,
        "verb_count": vcount,
        "verb_allowed": max_v,
        "verb_status": "valid" if vcount <= max_v else "invalid",
        "adverb_count": adverb_hits,
        "fact_anchor": fact_anchor,
        "classification": cls,
    }
    block["transformation_effects"] = _eval_transformation_rules(block, trans_ctx)
    return block


def clean_transcript(text: str) -> str:
    """Prefix-merge style dedupe for overlapping caption lines."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    out: list[str] = []
    for ln in lines:
        if not out:
            out.append(ln)
            continue
        last = out[-1]
        if ln == last:
            continue
        if ln.startswith(last):
            out[-1] = ln
        elif last.startswith(ln):
            continue
        else:
            out.append(ln)
    # join broken hyphenations common in ASR
    body = "\n".join(out)
    body = re.sub(r"(\w)(two|three)thirds", r"\1 \2 thirds", body, flags=re.I)
    return body


def match_training_examples(sentence: str, rules: dict) -> list[dict]:
    low = sentence.lower().strip().rstrip(".")
    hits = []
    for ex in rules.get("training_examples") or []:
        ph = (ex.get("phrase") or "").lower()
        if ph and ph in low:
            hits.append({"matched_phrase": ex.get("phrase"), "example": ex})
    return hits


def analyze_document(
    text: str,
    rules: dict,
    suffix_ctx: dict,
    prefix_ctx: dict,
    roots_ctx: dict,
    trans_ctx: dict,
    dict_ctx: dict,
) -> dict:
    sentences = _split_sentences(text)
    sents_out = []
    for s in sentences[:500]:
        block = classify_sentence(s, rules, suffix_ctx, prefix_ctx, roots_ctx, trans_ctx, dict_ctx)
        block["training_example_hits"] = match_training_examples(s, rules)
        sents_out.append(block)
    out: dict = {
        "parse": rules.get("parse"),
        "syntax": rules.get("syntax"),
        "grammar": rules.get("grammar"),
        "meta": rules.get("meta"),
        "sentence_count": len(sentences),
        "sentences_analyzed": len(sents_out),
        "sentences": sents_out,
    }
    if suffix_ctx.get("loaded"):
        out["suffix_engine"] = {
            "metadata": suffix_ctx.get("metadata"),
            "rule_groups": suffix_ctx.get("rule_groups"),
            "suffix_count": len(suffix_ctx.get("suffixes_sorted") or []),
        }
    else:
        out["suffix_engine"] = {"loaded": False, "hint": f"missing {SUFFIXES_PATH.name}"}
    if prefix_ctx.get("loaded"):
        out["prefix_engine"] = {
            "metadata": prefix_ctx.get("metadata"),
            "rule_groups": prefix_ctx.get("rule_groups"),
            "prefix_count": len(prefix_ctx.get("prefixes_sorted") or []),
        }
    else:
        out["prefix_engine"] = {"loaded": False, "hint": f"missing {PREFIXES_PATH.name}"}
    if roots_ctx.get("loaded"):
        out["roots_module"] = {
            "metadata": roots_ctx.get("metadata"),
            "root_count": len(roots_ctx.get("roots_sorted") or []),
        }
    else:
        out["roots_module"] = {"loaded": False, "hint": f"missing {ROOTS_PATH.name}"}
    if trans_ctx.get("loaded"):
        out["transformations_module"] = {
            "metadata": trans_ctx.get("metadata"),
            "rule_count": len(trans_ctx.get("rules") or []),
        }
    else:
        out["transformations_module"] = {"loaded": False, "hint": f"missing {TRANSFORMATIONS_PATH.name}"}
    if dict_ctx.get("loaded"):
        out["dictionary_module"] = {
            "metadata": dict_ctx.get("metadata"),
            "entry_count": dict_ctx.get("count", 0),
        }
    else:
        out["dictionary_module"] = {"loaded": False, "hint": f"missing {DICTIONARY_PATH.name}"}
    return out


def main() -> None:
    args: dict = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_parse_error": raw[:500]}

    rules = _load_rules()
    suffix_ctx = _load_suffix_engine()
    prefix_ctx = _load_prefix_engine()
    roots_ctx = _load_roots()
    trans_ctx = _load_transformations()
    dict_ctx = _load_dictionary()
    action = (args.get("action") or "analyze").strip().lower()
    path = (args.get("document_path") or args.get("path") or "").strip()
    text = (args.get("text") or "").strip()

    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            print(json.dumps({"ok": False, "error": f"document_path not found: {path}"}))
            return
        text = p.read_text(encoding="utf-8", errors="replace")

    if action == "rules":
        print(json.dumps({"ok": True, "rules": rules}, ensure_ascii=False, indent=2))
        return

    if action == "suffixes":
        full = bool(args.get("full"))
        payload: dict = {
            "ok": True,
            "metadata": suffix_ctx.get("metadata"),
            "rule_groups": suffix_ctx.get("rule_groups"),
            "loaded": suffix_ctx.get("loaded", False),
            "suffix_count": len(suffix_ctx.get("suffixes_sorted") or []),
        }
        if full:
            payload["suffixes"] = suffix_ctx.get("suffixes_sorted") or []
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if action == "prefixes":
        full = bool(args.get("full"))
        payload = {
            "ok": True,
            "metadata": prefix_ctx.get("metadata"),
            "rule_groups": prefix_ctx.get("rule_groups"),
            "loaded": prefix_ctx.get("loaded", False),
            "prefix_count": len(prefix_ctx.get("prefixes_sorted") or []),
        }
        if full:
            payload["prefixes"] = prefix_ctx.get("prefixes_sorted") or []
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if action == "roots":
        full = bool(args.get("full"))
        payload = {
            "ok": True,
            "metadata": roots_ctx.get("metadata"),
            "loaded": roots_ctx.get("loaded", False),
            "root_count": len(roots_ctx.get("roots_sorted") or []),
        }
        if full:
            payload["roots"] = roots_ctx.get("roots_sorted") or []
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if action == "transformations":
        print(
            json.dumps(
                {
                    "ok": True,
                    "metadata": trans_ctx.get("metadata"),
                    "loaded": trans_ctx.get("loaded", False),
                    "rules": trans_ctx.get("rules") or [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if action == "dictionary":
        full = bool(args.get("full"))
        payload = {
            "ok": True,
            "metadata": dict_ctx.get("metadata"),
            "loaded": dict_ctx.get("loaded", False),
            "entry_count": dict_ctx.get("count", 0),
        }
        if full:
            payload["entries"] = list((dict_ctx.get("by_word") or {}).values())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if action == "clean":
        if not text:
            print(json.dumps({"ok": False, "error": "clean requires **text** or **document_path**."}))
            return
        out = clean_transcript(text)
        max_chars = int(args.get("max_chars") or 500_000)
        truncated = len(out) > max_chars
        body = out[:max_chars] if truncated else out
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "clean",
                    "char_count": len(out),
                    "truncated": truncated,
                    "text": body,
                },
                ensure_ascii=False,
            )
        )
        return

    if action == "transform":
        if not text:
            print(json.dumps({"ok": False, "error": "transform requires **text** or **document_path**."}))
            return
        mode = (args.get("mode") or "suggestions").strip().lower()
        sents = _split_sentences(text)[:50]
        suggestions = []
        for s in sents:
            rep = classify_sentence(s, rules, suffix_ctx, prefix_ctx, roots_ctx, trans_ctx, dict_ctx)
            suggestions.append(
                {
                    "original": s,
                    "classification": rep["classification"],
                    "verb_count": rep["verb_count"],
                    "note": "No automatic rewrite; return tags only unless mode=experimental.",
                }
            )
        print(
            json.dumps(
                {"ok": True, "action": "transform", "mode": mode, "suggestions": suggestions},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if action == "pipeline":
        if not text:
            print(json.dumps({"ok": False, "error": "pipeline requires **text** or **document_path**."}))
            return
        cleaned = clean_transcript(text) if args.get("pre_clean") else text
        doc = analyze_document(cleaned, rules, suffix_ctx, prefix_ctx, roots_ctx, trans_ctx, dict_ctx)
        doc["pipeline"] = [
            "tokenize",
            "parts_of_speech",
            "verb_analysis",
            "prep_validation",
            "prefix_engine",
            "suffix_engine",
            "root_engine",
            "meaning_synthesis",
            "transformation_rules",
        ]
        doc["pre_clean_applied"] = bool(args.get("pre_clean"))
        print(json.dumps({"ok": True, **doc}, ensure_ascii=False, indent=2))
        return

    # default: analyze
    if not text:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Missing **text** or **document_path**. Actions: analyze | pipeline | rules | suffixes | prefixes | roots | transformations | dictionary | clean | transform.",
                }
            )
        )
        return

    out = analyze_document(text, rules, suffix_ctx, prefix_ctx, roots_ctx, trans_ctx, dict_ctx)
    print(json.dumps({"ok": True, "action": "analyze", **out}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
