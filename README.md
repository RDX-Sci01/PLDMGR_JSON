# PLDMGR_JSON

Custom payload repository for [PS5 Payload Manager](https://github.com/ps5-payload-dev/ps5-payload-manager).

## Adding this source

1. Open the Payload Manager dashboard on your PS5
2. Go to **Settings** → **Manage Sources**
3. Click **Add Source** and paste:
   ```
   https://raw.githubusercontent.com/RDX-Sci01/PLDMGR_JSON/main/payloads.json
   ```
4. Press **Add** — the repository will appear in your catalog

---

## For contributors / maintainers

### Adding or removing payloads

Edit [`links.txt`](./links.txt) — one direct download URL per line:

```
# Comments start with #
https://github.com/user/repo/releases/download/v1.0/payload.elf
https://example.com/payloads/tool.bin
```

Supported extensions: `.elf`, `.bin`, `.lua`

Commit and push. GitHub Actions will automatically regenerate `payloads.json` within seconds — no manual step needed.

### Running locally

```bash
python3 generate.py
```

This reads `links.txt` and writes `payloads.json` in the same directory.

To set a custom display name for the repository:

```bash
REPO_DISPLAY_NAME="My Repo Name" python3 generate.py
```

### URL format for GitHub releases

```
https://github.com/<user>/<repo>/releases/download/<tag>/<filename>
```

Example:
```
https://github.com/ItsBlurf/BFpilot/releases/download/v0.2.0/bfpilot.elf
```

---

## Payloads

| Name | Version | Description |
|------|---------|-------------|
| BFpilot | v0.2.0 | Lightweight PS5 browser-based file manager (port 5905) |

---

## How it works

```
links.txt  →  generate.py  →  payloads.json
               (GitHub Actions runs this automatically on every push)
```

`payloads.json` is served as a static file via GitHub's raw content CDN and consumed directly by Payload Manager.
