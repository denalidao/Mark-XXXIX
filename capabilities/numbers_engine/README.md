# numbers_engine

Routed capability (`capabilities/ROUTER.json`): **desktop**, no browser.

Loads the bundled cipher spec:

- `data/derived/numbers_engine/quantum_cipher_engine_methods.json`
- optional evidence index: `data/derived/numbers_engine/numbers_engine_corpus_snippets.json`

## stdin JSON

Pipe one JSON object to `run.py` (same pattern as `parse_syntax_grammar`).

| action | fields |
|--------|--------|
| `analyze` | `text` or `document_path` |
| `pipeline` | `text` or `document_path` — `analyze` plus `engine_pipeline` from spec |
| `spec` | optional `full: true` for entire JSON |
| `evidence` | optional `section_id` for one block; else list section ids |
| `lexicon` | none — dumps derived token hints + schema |
| `math_evaluate` | `expression` or `text`; optional `mode`: `syntax_first_order` (default) or `standard` |
| `monad` | `numbers`: array of numbers |

## CLI example (PowerShell)

```powershell
Set-Location Mark-XXXIX
'{"action":"math_evaluate","expression":"4 + 4 x 4","mode":"syntax_first_order"}' | python capabilities/numbers_engine/run.py
```

Or invoke `capabilities/numbers_engine/run.py` directly with JSON on stdin.

Mark tool: **`run_capability`** with `capability_id: numbers_engine`.
