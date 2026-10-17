#!/usr/bin/env python3
"""
Generate payloads.json for a PS5 Payload Manager custom repository.

Reads a list of payload sources from links.txt and produces payloads.json
conforming to the schema in:
https://github.com/itsPLK/ps5-payload-manager/blob/main/CUSTOM_REPOSITORIES.md

links.txt format
-----------------
One entry per line. Blank lines and lines starting with '#' are ignored.

    url
    url | category
    url | category | description
    url | category | description | version

Only `url` is required. Anything you omit is left out of the generated
entry (category falls back to "Uncategorized" per the spec; name and
filename are derived from the URL unless overridden with
`name=...` / `filename=...` key=value segments anywhere in the line).

Examples:
    https://example.com/payloads/ftpsrv_v0.19.elf
    https://example.com/payloads/debug_tool.elf | Networking
    https://example.com/payloads/goldhen.bin | Loaders | GoldHEN loader | v2.4b18
    https://example.com/p.elf | Tools | name=My Tool | filename=mytool.elf

REPO_NAME can be overridden via the REPO_NAME environment variable.
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"
REPO_NAME = os.environ.get("REPO_NAME", "Custom Payload Repository")
DEFAULT_CATEGORY = "Uncategorized"
USER_AGENT = "payload-catalog-generator/1.0"
CHUNK_SIZE = 1024 * 1024  # 1 MiB


def parse_line(line: str) -> dict:
    """Parse one links.txt line into a raw field dict."""
    parts = [p.strip() for p in line.split("|")]
    url = parts[0]

    fields = {"url": url}
    positional_keys = ["category", "description", "version"]
    positional_idx = 0

    for part in parts[1:]:
        if "=" in part and part.split("=", 1)[0].strip().lower() in (
            "name",
            "filename",
            "category",
            "description",
            "version",
        ):
            key, _, value = part.partition("=")
            fields[key.strip().lower()] = value.strip()
        else:
            if positional_idx < len(positional_keys):
                fields.setdefault(positional_keys[positional_idx], part)
                positional_idx += 1

    return fields


def read_links(path: str) -> list:
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(parse_line(line))
    return entries


def filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path)
    return name or "payload.bin"


def sha256_of_url(url: str) -> str:
    """Stream-download the file and return its SHA-256 hex digest."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(req) as response:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_payload_entry(fields: dict) -> dict:
    url = fields["url"]
    name = fields.get("name") or filename_from_url(url).rsplit(".", 1)[0]
    filename = fields.get("filename") or filename_from_url(url)

    entry = {
        "name": name,
        "filename": filename,
        "url": url,
    }

    if fields.get("description"):
        entry["description"] = fields["description"]
    if fields.get("version"):
        entry["version"] = fields["version"]
    entry["category"] = fields.get("category") or DEFAULT_CATEGORY

    try:
        entry["checksum"] = sha256_of_url(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not checksum {url}: {exc}", file=sys.stderr)

    return entry


def main() -> int:
    if not os.path.exists(LINKS_FILE):
        print(f"error: {LINKS_FILE} not found", file=sys.stderr)
        return 1

    raw_entries = read_links(LINKS_FILE)
    if not raw_entries:
        print(f"error: no payload entries found in {LINKS_FILE}", file=sys.stderr)
        return 1

    payloads = []
    for fields in raw_entries:
        print(f"processing {fields['url']}")
        payloads.append(build_payload_entry(fields))

    # "name" must be inserted before "payloads" — dict insertion order
    # is preserved by json.dump, and the parser reads this positionally.
    catalog = {
        "name": REPO_NAME,
        "payloads": payloads,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {len(payloads)} payload(s) to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
