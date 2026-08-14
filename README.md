# PLDMGR_JSON

Custom payload repository for [PS5 Payload Manager](https://github.com/ps5-payload-dev/ps5-payload-manager).  
Payloads are **automatically kept up to date** — the list refreshes every 2 hours via GitHub Actions.

## Adding this source

1. Open the Payload Manager dashboard on your PS5
2. Go to **Settings** → **Manage Sources**
3. Click **Add Source** and paste:
   ```
   https://raw.githubusercontent.com/RDX-Sci01/PLDMGR_JSON/main/payloads.json
   ```
4. Press **Add** — the repository will appear in your catalog

---

## Payloads

| Name | Version | Description |
|------|---------|-------------|
| BFpilot | latest | Lightweight PS5 browser-based file manager (port 5905) |

---

## For maintainers

### Adding a payload

Edit [`links.txt`](./links.txt). Two formats are supported:

**GitHub repo** (recommended — auto-resolves the latest release asset):
```
github:ItsBlurf/BFpilot
github:someuser/somerepo
```

**Direct URL** (pinned — will not auto-update):
```
https://example.com/payloads/tool.elf
```

Commit and push. The Action regenerates `payloads.json` automatically.

### Auto-update schedule

`payloads.json` is regenerated:
- **Every 2 hours** — picks up new upstream releases without any manual action
- On every push that changes `links.txt`
- On demand via the **Actions** tab → **Run workflow**

Each run also **validates every download URL** with a live HTTP check before writing. Any payload whose URL returns an error is skipped and logged — the rest are still written. The file is only updated if something actually changed, keeping the commit history clean.

### Running locally

```bash
python3 generate.py
```

Requires internet access to hit the GitHub API for `github:` entries and to validate URLs.  
Set `REPO_DISPLAY_NAME` to override the catalog title:

```bash
REPO_DISPLAY_NAME="My Repo" python3 generate.py
```

### How it works

```
links.txt
  │
  ├─ github:<user>/<repo>  →  GitHub API (latest release)
  │                               └─ resolves filename, URL, version, description
  │
  └─ https://...           →  used as-is (pinned URL)
  │
  ├─ HEAD request to each URL  →  skip if unreachable (404, offline, etc.)
  │
  └──▶  payloads.json  (committed back by GitHub Actions only if changed)
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success — `payloads.json` written |
| `1` | `links.txt` is empty |
| `2` | All entries failed to resolve or validate |