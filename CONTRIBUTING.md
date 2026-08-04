# Contributing

## Setup

```bash
git clone <this-repo>
cd local-ai-monitor
bash scripts/install.sh
make -C native all
bash menubar/scripts/build.sh   # optional rebuild of glass app
python3 -m unittest discover -s tests -q
```

## Rules of the road

1. **Do not commit** `RemoteApp/local.xcconfig` or any Apple Team ID.
2. **Do not** reintroduce free-page-only auto-kill of interactive AI sessions.
3. Prefer **lean always-on** (`local-ai-monitord`) over Python KeepAlive collect.
4. User-facing copy: no raw feed filenames, no internal “lab” labels on production paths.
5. Tests should use generic paths (`/Users/u/...`), not personal home directories.

## Pull requests

- Keep scope tight; include a short “why” and how you tested.
- If you change resource policy, note thrash vs headroom behavior explicitly.
