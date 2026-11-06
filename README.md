# fanedit-external-index

External fan-edit index for the Optimus Discord bot — fan edits that live
**outside** the Fan Edit Fan Club collection, built from the club's
Master Links document.

Live index: https://faneditfanclub.github.io/fanedit-external-index/external-index.json

Rebuilt hourly by GitHub Actions (`.github/workflows/build-index.yml`).

## Files

- `sources.json` — one entry per Master Links source (label, URL, adapter,
  type). Out-of-scope, skipped, duplicate, and search-template entries are
  recorded here with their reason instead of being silently dropped.
- `build.py` — the builder (Python standard library only, no secrets).
  Reads `sources.json`, runs one adapter per source, dedups by normalized
  title, drops anything already in the collection (checked against the
  public collection site index), and writes `external-index.json`.
- `external-index.json` — the published index (compact JSON).

## Record shape

```json
{"title": "...", "editor": "...", "original": "...", "source": "...",
 "url": "...", "link_kind": "drive|cloud|doc|sheet|website",
 "kind": "file|folder"}
```

## Adapters

Core adapters live in `build.py` (`CORE_ADAPTERS`): `drive_folder`,
`drive_file`, `sheet`, `doc`, `pastebin`, `rss`.

Extra adapters (`website`, `mega`, `gofile`, `yandex`) live in
`adapters_extra.py` (owned by a second agent) and are imported if present:

```python
def adapt(source: dict, ctx: dict) -> list[dict]:
    # ctx: {"fetch_text": callable(url, timeout=30) -> str,
    #       "fetch_bytes": callable(url, timeout=30) -> bytes,
    #       "log": callable(str) -> None}
    # Return records; on an unparseable/gated source return [] and
    # ctx["log"]("SKIP <label>: <reason>").
```

## Rate limits

The builder hits ~90 sources every hour, including Google's unofficial
endpoints (embedded folder views, gviz CSV). A 429 / rate-limit response
is a **logged skip, never a build failure** — the source is retried on
the next run. Drive folders are capped (200 folder fetches per source,
depth 4) and the whole run has a soft 45-minute budget so it always
finishes inside the hour.
