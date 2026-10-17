#!/usr/bin/env python3
"""
Generate payloads.json for a PS5 Payload Manager custom repository.

Matches the richer field set used by itsPLK/ps5-payloads-mirror
(payloads_v2.json): name, filename, url, source, source_direct,
description, last_update, version, category, checksum — while NOT
mirroring binaries. `url` and `source_direct` both point straight at
the upstream release asset; `source` is the upstream repo's releases
page. If you later want to actually mirror binaries into a GitHub
Release of your own, `url` would need to change to point there instead
— that's a separate, bigger change (download + re-upload as a release
asset), not something this script does.

links.txt format
-----------------
One entry per line. Blank lines and lines starting with '#' are ignored.

1. GitHub repo shorthand — resolved against that repo's LATEST release,
   one payload entry per matching release asset:

       github:owner/repo
       github:owner/repo | category
       github:owner/repo | category | description | version

   Key=value overrides (any order, anywhere in the line):
     name=...            override payload name
     filename=...         override filename
     asset_pattern=...    only use assets whose filename contains this
                           substring (case-insensitive) — use when a
                           release has multiple assets and you only
                           want one, e.g. asset_pattern=ps5
     extract_file=...     informational only: the file inside a zip/tar
                           asset that is the actual payload to run.
                           Recorded on the entry if the asset itself
                           isn't a raw payload file.

   `version` defaults to the release tag; `description` defaults to the
   release name; `last_update` is always taken from the release's
   publish date (UTC, YYYY-MM-DD) and cannot be overridden.

2. A direct HTTP(S) URL:

       https://example.com/payloads/ftpsrv_v0.19.elf
       https://example.com/payloads/ftpsrv_v0.19.elf | category | description | version

   `source` and `source_direct` are set equal to `url` itself (there's
   no separate upstream to point to), and `last_update` is omitted
   since no release metadata is available for a bare URL.

Only .elf and .bin files are included in payloads.json. For
`github:owner/repo` entries, non-.elf/.bin release assets are filtered
out before anything else (including asset_pattern matching); if a
release has none, that repo is skipped with a warning. For direct URL
entries, a URL whose filename doesn't end in .elf or .bin is skipped
with a warning rather than included.

Icons: mirrors the real algorithm found in itsPLK/ps5-payloads-mirror's
set_icon.py (get_suggested_icon). For each payload with a GitHub
`source`, we check a handful of common asset paths on that repo's main
branch (assets/icon0.png, icon0.png, assets/icon.png, icon.png,
assets/logo.png, logo.png) via raw.githubusercontent.com; the first one
that exists is used. If none exist, we fall back to the repo owner's
GitHub avatar (https://github.com/<owner>.png) — this matches
itsPLK's data, where unrelated repos under the same GitHub org end up
sharing one icon_key because they share one avatar image. `icon_key` is
the SHA-256 of the downloaded image's raw bytes, so identical images
naturally dedupe to the same key, same as the upstream data shows.
Entries from a bare direct URL (no GitHub `source` to search) get no
icon unless manually overridden with `icon=<url>` in links.txt.
icons.json ends up as {"icons": {<icon_key>: "data:image/<type>;base64,..."}},
referenced from payloads.json via top-level "icons_url" and per-entry
"icon_key".

REPO_NAME overrides the top-level catalog name via env var.
GITHUB_TOKEN, if set, authenticates GitHub API calls.
"""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"
ICONS_FILE = "icons.json"
REPO_NAME = os.environ.get("REPO_NAME", "Custom Payload Repository")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
DEFAULT_CATEGORY = "Uncategorized"
USER_AGENT = "payload-catalog-generator/5.0"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
GITHUB_API = "https://api.github.com"

OVERRIDE_KEYS = (
    "name",
    "filename",
    "category",
    "description",
    "version",
    "asset_pattern",
    "extract_file",
    "icon",
)

PAYLOAD_EXTENSIONS = (".elf", ".bin")

ICON_ASSET_PATHS = (
    "assets/icon0.png",
    "icon0.png",
    "assets/icon.png",
    "icon.png",
    "assets/logo.png",
    "logo.png",
)

MIME_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"<?xml", "image/svg+xml"),
    (b"<svg", "image/svg+xml"),
)

_icon_cache = {}  # source URL -> (icon_key, data_uri), avoids refetching shared avatars


def sniff_mime(data: bytes) -> str:
    for sig, mime in MIME_SIGNATURES:
        if data.startswith(sig):
            return mime
    return "application/octet-stream"


def url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def find_repo_icon_url(owner: str, repo: str) -> str:
    """Check common asset paths on the repo's main branch; fall back to
    the owner's GitHub avatar. Mirrors set_icon.py's get_suggested_icon()."""
    for path in ICON_ASSET_PATHS:
        candidate = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
        if url_exists(candidate):
            return candidate
    return f"https://github.com/{owner}.png"


def fetch_icon(icon_url: str):
    """Download icon_url and return (icon_key, data_uri), or None on failure.
    Cached by icon_url since many repos share one owner avatar."""
    if icon_url in _icon_cache:
        return _icon_cache[icon_url]

    req = urllib.request.Request(icon_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not fetch icon {icon_url}: {exc}", file=sys.stderr)
        return None

    mime = sniff_mime(data)
    icon_key = hashlib.sha256(data).hexdigest()
    data_uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    result = (icon_key, data_uri)
    _icon_cache[icon_url] = result
    return result


def icon_for_entry(entry: dict, fields: dict, icons: dict) -> None:
    """Attach icon_key to entry and record it in icons, if an icon can be found."""
    icon_url = fields.get("icon")
    if not icon_url:
        source = entry.get("source", "")
        match = re.search(r"https?://github\.com/([^/]+)/([^/]+)", source)
        if not match:
            return
        owner, repo = match.group(1), match.group(2).rstrip("/")
        icon_url = find_repo_icon_url(owner, repo)

    result = fetch_icon(icon_url)
    if not result:
        return
    icon_key, data_uri = result
    entry["icon_key"] = icon_key
    icons[icon_key] = data_uri


def gh_request(path: str) -> dict:
    url = f"{GITHUB_API}{path}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_line(line: str) -> dict:
    parts = [p.strip() for p in line.split("|")]
    source = parts[0]

    fields = {"source_entry": source}
    positional_keys = ["category", "description", "version"]
    positional_idx = 0

    for part in parts[1:]:
        if "=" in part and part.split("=", 1)[0].strip().lower() in OVERRIDE_KEYS:
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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(req) as response:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def checksum_for_asset(asset: dict) -> str:
    digest = asset.get("digest")  # e.g. "sha256:abc123..."
    if digest and digest.startswith("sha256:"):
        return digest.split(":", 1)[1]
    try:
        return sha256_of_url(asset["browser_download_url"])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"warning: could not checksum {asset['browser_download_url']}: {exc}", file=sys.stderr)
        return ""


def release_date(release: dict) -> str:
    raw = release.get("published_at") or release.get("created_at")
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%d")


def build_entries_from_github(fields: dict) -> list:
    owner_repo = fields["source_entry"].split("github:", 1)[1].strip()
    print(f"resolving github:{owner_repo} (latest release)")

    try:
        release = gh_request(f"/repos/{owner_repo}/releases/latest")
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"warning: could not fetch latest release for {owner_repo}: {exc}", file=sys.stderr)
        return []

    assets = release.get("assets", [])
    if not assets:
        print(f"warning: {owner_repo} latest release has no downloadable assets, skipping", file=sys.stderr)
        return []

    assets = [a for a in assets if a["name"].lower().endswith(PAYLOAD_EXTENSIONS)]
    if not assets:
        print(f"warning: {owner_repo} latest release has no .elf/.bin assets, skipping", file=sys.stderr)
        return []

    pattern = fields.get("asset_pattern", "").lower()
    if pattern:
        assets = [a for a in assets if pattern in a["name"].lower()]
        if not assets:
            print(f"warning: no .elf/.bin assets of {owner_repo} match asset_pattern={pattern!r}, skipping", file=sys.stderr)
            return []

    repo_name = owner_repo.split("/", 1)[1]
    category = fields.get("category") or DEFAULT_CATEGORY
    version = fields.get("version") or release.get("tag_name") or ""
    description = fields.get("description") or (release.get("name") or "").strip()
    last_update = release_date(release)
    source_page = f"https://github.com/{owner_repo}/releases"

    entries = []
    for asset in assets:
        direct_url = asset["browser_download_url"]
        entry = {
            "name": fields.get("name") or (repo_name if len(assets) == 1 else asset["name"]),
            "filename": fields.get("filename") or asset["name"],
            "url": direct_url,
            "source": source_page,
            "source_direct": direct_url,
        }
        if description:
            entry["description"] = description
        entry["last_update"] = last_update
        if version:
            entry["version"] = version
        entry["category"] = category
        if fields.get("extract_file"):
            entry["extract_file"] = fields["extract_file"]
        if pattern:
            entry["asset_pattern"] = fields["asset_pattern"]

        checksum = checksum_for_asset(asset)
        if checksum:
            entry["checksum"] = checksum

        entries.append(entry)

    return entries


def build_entry_from_url(fields: dict):
    url = fields["source_entry"]
    name = fields.get("name") or filename_from_url(url).rsplit(".", 1)[0]
    filename = fields.get("filename") or filename_from_url(url)

    if not filename.lower().endswith(PAYLOAD_EXTENSIONS):
        print(f"warning: {url} is not a .elf/.bin file (filename: {filename!r}), skipping", file=sys.stderr)
        return None

    entry = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": url,
        "source_direct": url,
    }
    if fields.get("description"):
        entry["description"] = fields["description"]
    if fields.get("version"):
        entry["version"] = fields["version"]
    entry["category"] = fields.get("category") or DEFAULT_CATEGORY
    if fields.get("extract_file"):
        entry["extract_file"] = fields["extract_file"]

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
    entry_fields = []  # parallel list: originating links.txt fields for each entry
    for fields in raw_entries:
        source = fields["source_entry"]
        if source.startswith("github:"):
            new_entries = build_entries_from_github(fields)
            payloads.extend(new_entries)
            entry_fields.extend([fields] * len(new_entries))
        else:
            print(f"processing {source}")
            entry = build_entry_from_url(fields)
            if entry is not None:
                payloads.append(entry)
                entry_fields.append(fields)

    if not payloads:
        print("error: no payload entries could be resolved", file=sys.stderr)
        return 1

    icons = {}
    for entry, fields in zip(payloads, entry_fields):
        icon_for_entry(entry, fields, icons)

    catalog = {
        "name": REPO_NAME,
        "icons_url": ICONS_FILE,
        "payloads": payloads,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(ICONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"icons": icons}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {len(payloads)} payload(s) to {OUTPUT_FILE}")
    print(f"wrote {len(icons)} icon(s) to {ICONS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
