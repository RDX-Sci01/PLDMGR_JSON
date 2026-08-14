# PLDMGR_JSON

Custom payload repository for PS5 Payload Manager. Payloads are automatically kept up to date — the list refreshes every 2 hours via GitHub Actions.

## Adding this source

1. Open the Payload Manager dashboard on your PS5
2. Go to **Settings → Manage Sources**
3. Click **Add Source** and paste:
   ```
   https://raw.githubusercontent.com/RDX-Sci01/PLDMGR_JSON/main/payloads.json
   ```
4. Press **Add** — the repository will appear in your catalog

---

## For maintainers

### Adding a payload

Edit [`links.txt`](links.txt). Three formats are supported:

**GitHub repo** — auto-resolves the latest release asset, updates automatically:
```
github:ItsBlurf/BFpilot
```

**Mirror** — bulk-imports every `.elf`/`.bin`/`.lua` asset from a specific release tag at once:
```
mirror:itsPLK/ps5-payloads-mirror@payloads-mirror
```

**Direct URL** — pinned, will not auto-update:
```
https://example.com/payloads/tool.elf
```

Commit and push. The Action regenerates `payloads.json` automatically.

> **Note on duplicates:** `links.txt` currently includes both the `mirror:` entry and individual `github:` entries for the same payloads. This is intentional — the mirror provides a stable hosted copy, while the `github:` entries track upstream directly. If you want to avoid duplicates in `payloads.json`, remove the `github:` entries for anything already covered by the mirror.

---

## Auto-update schedule

`payloads.json` is regenerated:

- **Every 2 hours** — picks up new upstream releases without any manual action
- **On every push** that changes `links.txt`
- **On demand** via the Actions tab → Run workflow

Each run also validates every download URL with a live HTTP check before writing. Any payload whose URL returns an error is skipped and logged — the rest are still written. The file is only updated if something actually changed, keeping the commit history clean.

---

## Running locally

```bash
python3 generate.py
```

Requires internet access to hit the GitHub API for `github:` entries and to validate URLs.

Set `REPO_DISPLAY_NAME` to override the catalog title:

```bash
REPO_DISPLAY_NAME="My Repo" python3 generate.py
```

---

## How it works

```
links.txt
│
├─ github:<user>/<repo>  →  GitHub API (latest release)
│                            └─ resolves filename, URL, version, description
│
├─ mirror:<user>/<repo>@<tag>  →  GitHub API (release assets) or hosted JSON
│
└─ https://...           →  used as-is (pinned URL)
│
├─ HEAD request to each URL  →  skip if unreachable (404, offline, etc.)
│
└──▶  payloads.json  (committed back by GitHub Actions only if changed)
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success — `payloads.json` written |
| 1 | `links.txt` is empty or missing |
| 2 | All entries failed to resolve or validate |