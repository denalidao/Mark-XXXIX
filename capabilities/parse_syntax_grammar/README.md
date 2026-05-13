# Parse Syntax Grammar (PSG)

Local **deterministic** JSON analyzer aligned with transcript-derived “parse / syntax / grammar” framing. **Not** validated linguistics or law — use for tagging, RAG chunks, and side-by-side comparison with conventional NLP.

## Router

`parse_syntax_grammar` in `capabilities/ROUTER.json` (`browser: desktop`, no URL).

## `run.py` (stdin JSON → stdout JSON)

| `action` | Purpose |
|----------|---------|
| `analyze` (default) | Sentence split → token heuristics → verb count, prep anchor, vowel/prefix flags, **prefix/suffix engines**, `training_examples` substring hits, classification. |
| `pipeline` | Same as analyze after optional `pre_clean: true` (caption-style dedupe). |
| `rules` | Dump bundled `rules.json`. |
| `suffixes` | Dump `suffixes.json` metadata + counts; `"full": true` includes all suffix entries. |
| `prefixes` | Dump `prefixes.json` metadata + counts; `"full": true` includes all prefix entries. |
| `clean` | Transcript-style line dedupe + light normalization; optional `max_chars` (default 500000). |
| `transform` | Per-sentence tags only (no silent rewrite); `mode` reserved. |

**Inputs:** `text` **or** `document_path` (absolute path to `.txt`).

**Example (PowerShell):**

```powershell
'{ "capability_id": "parse_syntax_grammar", "action": "analyze", "text": "The corporation shall be operating and maintaining property." }' | python capabilities/parse_syntax_grammar/run.py
```

Mark tool: **`run_capability`** with `capability_id: parse_syntax_grammar`.

## Data

- **`rules.json`** — machine-readable rules + `training_examples` from your extraction spec.
- **`suffixes.json`** — Suffix engine: longest end-of-lemma match; tokens get `suffix_engine` (+ `contract_state_suffix` when applicable). Dump with `action: suffixes` (`full: true` for full array).
- **`prefixes.json`** — Prefix engine (`metadata`, `rule_groups`, `prefixes[]`). Longest start-of-lemma match; tokens get `prefix_engine` and `negation_prefix_claim` when class includes `negation`. Dump with `action: prefixes` (`full: true` for full array).
- Large seminar transcript: `data/transcripts/J7-Rknkrnts_*.txt` (use `document_path` + `action: pipeline`, `pre_clean: true` for long files; cap sentences in code is 500 per run — raise in `run.py` if needed).

## Next steps (optional)

- Wire **spaCy** / NLTK for POS instead of heuristics.
- Chunk long documents and merge JSON reports.
- Add `comparison` mode: run heuristics + store alongside classical parser output.
