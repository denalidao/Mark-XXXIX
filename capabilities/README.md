# Capabilities (markdown + scripts)

Each subfolder is one **site or app** you automate. **`ROUTER.json`** is the allowlist: only those ids can run via the **`run_capability`** tool.

## Layout

- `ROUTER.json` — id → `browser`, `start_url`, optional `delegate`
- `<id>/README.md` — intent, examples, caveats (session cookies in your real browser profile)
- `<id>/run.py` — optional entry script; stdin is one JSON object of tool args (skipped if `delegate` is set in the router)

## Delegated capabilities

- **`youtube`** — router `delegate: youtube_video`; `run_capability` with `query` runs the existing player/search path.

## Folder `run.py` playbooks

- **`proton_mail`** — real playbook in `proton_mail/run.py` (no `delegate`); see that folder’s `README.md`.

## Adding a new capability

1. Add a key under `capabilities` in `ROUTER.json`
2. Create the folder + `README.md`
3. Add `run.py` that reads stdin JSON and prints a final JSON line or human text for logs

Mark invokes **`run_capability`** with `capability_id` plus any extra fields (e.g. `query` for YouTube).
