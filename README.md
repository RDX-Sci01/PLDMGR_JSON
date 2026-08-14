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