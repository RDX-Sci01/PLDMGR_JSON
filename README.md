# PLDMGR_JSON

Custom payload repository for PS5 Payload Manager.

Payload sources are automatically refreshed every 2 hours using GitHub Actions. The generated `payloads.json` contains validated payload download URLs and metadata.

## Adding this source

Open the Payload Manager dashboard on your PS5:

1. Go to **Settings → Manage Sources**
2. Click **Add Source**
3. Paste **one** of the following URLs:

### GitHub Raw

```text
https://raw.githubusercontent.com/RDX-Sci01/PLDMGR_JSON/main/payloads.json
```

### GitHub Pages

```text
https://rdx-sci01.github.io/PLDMGR_JSON/payloads.json
```

These are alternative URLs pointing to the same generated catalog. You normally only need to add one.

---

## For maintainers

### Adding a payload

Edit [`links.txt`](links.txt).

Three source formats are supported.

### GitHub repository

Automatically tracks the repository's latest published GitHub release:

```text
github:ItsBlurf/BFpilot
```

The generator looks for supported release assets:

* `.elf`
* `.bin`
* `.lua`

The selected release asset is added to `payloads.json` with its filename, download URL, release version, release date, description, and automatically detected category.

### Mirror

Bulk-imports supported payload assets from a specific release:

```text
mirror:itsPLK/ps5-payloads-mirror@payloads-mirror
```

For the `itsPLK/ps5-payloads-mirror` source, the generator first attempts to use its published `payloads.json`. If that cannot be retrieved, it falls back to the GitHub release API.

Generic mirror sources use the GitHub release API directly.

### Direct URL

Adds a pinned payload URL without automatic release tracking:

```text
https://example.com/payloads/tool.elf
```

Direct URLs are preserved as specified and are marked as `pinned`.

After changing `links.txt`, commit and push the changes. GitHub Actions automatically regenerates `payloads.json`.

---

## Duplicate sources

It is possible to have both:

```text
mirror:itsPLK/ps5-payloads-mirror@payloads-mirror
```

and individual:

```text
github:user/repository
```

entries.

This can be useful when the mirror provides a stable hosted copy while the GitHub entry tracks the upstream release directly.

The generator automatically removes duplicate entries when they resolve to the same normalized download URL.

Different download URLs are intentionally retained, even when they represent the same payload.

If you do not want both copies in the catalog, remove the corresponding `github:` entry from `links.txt`.

---

## Automatic updates

`payloads.json` is regenerated:

* **Every 2 hours** — checks configured GitHub repositories for newer releases
* **When `links.txt` changes** — regenerates the catalog after a push
* **Manually** — through **Actions → Generate payloads.json → Run workflow**

During each generation:

1. `links.txt` is parsed.
2. GitHub repositories are resolved through the GitHub API.
3. Mirror sources are resolved.
4. Direct URLs are accepted as pinned sources.
5. Entries are normalized and deduplicated.
6. Download URLs are validated.
7. Invalid or unreachable payload URLs are skipped.
8. Entries are sorted deterministically.
9. The generated JSON is validated.
10. `payloads.json` is only replaced when its contents have changed.

URL validation first attempts an HTTP `HEAD` request. If the server does not support `HEAD`, the generator falls back to a small `GET` request.

A failure for one payload does not prevent valid payloads from being generated.

---

## GitHub Actions

The repository uses GitHub Actions to run the generator automatically.

The workflow follows this process:

```text
GitHub Actions
      │
      ├── sync_links.py
      │       │
      │       └── updates managed repository entries
      │
      ├── generate.py
      │       │
      │       ├── GitHub API
      │       ├── mirror sources
      │       └── direct URLs
      │
      ├── validates payload URLs
      │
      └── payloads.json
              │
              └── committed if changed
```

The workflow requires permission to write repository contents:

```yaml
permissions:
  contents: write
```

A GitHub Actions `GITHUB_TOKEN` is automatically provided to the scripts for GitHub API requests.

---

## Running locally

Run:

```bash
python3 generate.py
```

The generator requires internet access for:

* GitHub API requests
* mirror sources
* payload URL validation

A GitHub token can optionally be provided to increase GitHub API rate limits:

```bash
export GITHUB_TOKEN="your_token"
python3 generate.py
```

No third-party Python packages are required.

---

## Source formats

### GitHub repository

```text
github:<user>/<repo>
```

Example:

```text
github:ItsBlurf/BFpilot
```

Tracks the latest published GitHub release.

### Mirror

```text
mirror:<user>/<repo>@<tag>
```

Example:

```text
mirror:itsPLK/ps5-payloads-mirror@payloads-mirror
```

Imports supported payload assets from the specified release.

### Direct URL

```text
https://...
```

Example:

```text
https://example.com/payloads/tool.elf
```

Pinned URL with no automatic release resolution.

---

## Supported payload formats

The generator currently accepts:

```text
.elf
.bin
.lua
```

Other release assets are ignored.

---

## Output

The final catalog is written to:

```text
payloads.json
```

Each payload contains metadata similar to:

```json
{
  "name": "example",
  "filename": "example.elf",
  "url": "https://example.com/example.elf",
  "source": "https://github.com/example/example/releases",
  "source_direct": "https://example.com/example.elf",
  "description": "Example payload",
  "last_update": "2026-08-14",
  "version": "v1.0.0",
  "category": "Utilities & Tools"
}
```

---

## Categories

Payload categories are automatically detected from the payload name and description.

Current categories include:

* **System & Jailbreak**
* **Networking & Servers**
* **Loaders**
* **Utilities & Tools**

If no matching keywords are found, the payload is assigned to **Utilities & Tools**.

---

## Error handling

Individual source failures do not normally stop the entire generation process.

For example, if one GitHub repository is unavailable but five other sources resolve successfully, the five valid sources can still be written to `payloads.json`.

During URL validation, unreachable payload URLs are skipped.

The generator exits with code `2` only when no usable payloads remain after resolution/validation.

---

## Deterministic generation

The generator is designed to produce stable output.

Entries are:

* normalized
* deduplicated
* sorted consistently
* validated before writing

The output file is only replaced when its contents actually change.

This prevents unnecessary commits when scheduled GitHub Actions runs produce the same catalog.

---

## Safe file updates

`payloads.json` is written atomically.

The generator creates and validates the new file before replacing the existing output. This prevents an interrupted generation from leaving a partially written `payloads.json`.

---

## Exit codes

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| `0`  | Success — `payloads.json` was written or was already up to date      |
| `1`  | `links.txt` is missing or contains no usable entries                 |
| `2`  | No payloads could be resolved or all resolved URLs failed validation |

---

## Repository structure

```text
PLDMGR_JSON/
├── .github/
│   └── workflows/
│       └── generate.yml
├── generate.py
├── sync_links.py
├── links.txt
├── payloads.json
└── README.md
```

---

## License

This repository contains source references and generated metadata for payload distribution.

Individual payloads remain subject to the licenses and terms of their respective upstream projects.

Please refer to each upstream repository for its applicable license and usage requirements.
