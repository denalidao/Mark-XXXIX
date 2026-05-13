# YouTube (Microsoft Edge)

- **Browser:** Edge (your profile with cookies).
- **Router:** `delegate: youtube_video` — **`run_capability`** with **`capability_id: youtube`** and **`query`** uses the existing Mark player (e.g. “gospel hip hop”).
- **Also:** you can still call **`youtube_video`** directly with `action: play` and `query`.

## Example (voice → tool)

`run_capability` with `capability_id: youtube`, `query: gospel hip hop`

No `run.py` here while delegation is active.
