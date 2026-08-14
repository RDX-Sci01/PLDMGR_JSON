#!/usr/bin/env python3
"""
generate.py — builds payloads.json from links.txt

Exit codes:
  0 — success, payloads.json written
  1 — links.txt is empty or missing
  2 — all entries failed to resolve or validate
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ASSET_EXTENSIONS = (".elf", ".bin", ".lua")
OUTPUT_FILE = "payloads.json"
LINKS_FILE = "links.txt"

# Category guessing keywords
CATEGORY_RULES = [
    (["jailbreak", "kstuff", "etahen", "hen", "exploit", "lapy", "daemon"], "System & Jailbreak"),
    (["ftp", "http", "web", "server", "srv", "dns", "upload", "network", "telnet", "shell"], "Networking & Servers"),
    (["loader", "elfldr", "linux", "autoloader", "webkit"], "Loaders"),
]
DEFAULT_CATEGORY = "Utilities & Tools"


def gh_headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def fetch_json(url):
    req = urllib.request.Request(url, headers=gh_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def head_ok(url):
    """Return True if URL responds with 2xx/3xx."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 400
    except Exception:
        return False


def guess_category(name, description=""):
    text = (name + " " + description).lower()
    for keywords, cat in CATEGORY_RULES:
        if any(k in text for k in keywords):
            return cat
    return DEFAULT_CATEGORY


def parse_version(tag):
    """Strip leading 'v' for display consistency."""
    return tag if tag else "unknown"


def asset_is_payload(name):
    return name.lower().endswith(ASSET_EXTENSIONS)


def date_from_iso(iso):
    if not iso:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return iso[:10]


# ── Resolvers ────────────────────────────────────────────────────────────────

def resolve_github(user_repo):
    """Fetch latest release from a GitHub repo."""
    url = f"{GITHUB_API}/repos/{user_repo}/releases/latest"
    try:
        rel = fetch_json(url)
    except Exception as e:
        print(f"  [WARN] github:{user_repo} — API error: {e}")
        return []

    assets = [a for a in rel.get("assets", []) if asset_is_payload(a["name"])]
    if not assets:
        print(f"  [WARN] github:{user_repo} — no .elf/.bin/.lua in latest release")
        return []

    # Pick the first matching asset (most repos ship one)
    asset = assets[0]
    tag = rel.get("tag_name", "")
    repo_name = user_repo.split("/")[-1]
    description = (rel.get("body") or "").strip().splitlines()[0][:120] if rel.get("body") else ""

    entry = {
        "name": repo_name,
        "filename": asset["name"],
        "url": asset["browser_download_url"],
        "source": f"https://github.com/{user_repo}/releases",
        "source_direct": asset["browser_download_url"],
        "description": description or f"{repo_name} payload",
        "last_update": date_from_iso(rel.get("published_at")),
        "version": parse_version(tag),
        "category": guess_category(repo_name, description),
    }
    return [entry]


def resolve_mirror(user_repo_tag):
    """
    mirror:user/repo@tag — fetch all payload assets from a specific release tag.
    Matches the structure of itsPLK/ps5-payloads-mirror.
    """
    # First try to fetch payloads.json from GitHub Pages if this is itsPLK's mirror
    if "itsPLK/ps5-payloads-mirror" in user_repo_tag or user_repo_tag.startswith("itsPLK/ps5-payloads-mirror"):
        try:
            entries = fetch_json("https://itsplk.github.io/ps5-payloads-mirror/payloads.json")
            print(f"  [OK] mirror — imported {len(entries)} entries from itsPLK payloads.json")
            return entries
        except Exception as e:
            print(f"  [WARN] mirror — could not fetch itsPLK payloads.json: {e}, falling back to API")

    # Generic mirror: fetch all assets from the release tag
    match = re.match(r"([^@]+)@(.+)", user_repo_tag)
    if not match:
        print(f"  [WARN] mirror:{user_repo_tag} — invalid format, expected user/repo@tag")
        return []

    user_repo, tag = match.group(1), match.group(2)
    url = f"{GITHUB_API}/repos/{user_repo}/releases/tags/{tag}"
    try:
        rel = fetch_json(url)
    except Exception as e:
        print(f"  [WARN] mirror:{user_repo_tag} — API error: {e}")
        return []

    entries = []
    for asset in rel.get("assets", []):
        if not asset_is_payload(asset["name"]):
            continue
        name = re.sub(r"[_\-]v?\d.*$", "", asset["name"].rsplit(".", 1)[0])
        entry = {
            "name": name,
            "filename": asset["name"],
            "url": asset["browser_download_url"],
            "source": f"https://github.com/{user_repo}/releases",
            "source_direct": asset["browser_download_url"],
            "description": f"{name} payload",
            "last_update": date_from_iso(rel.get("published_at")),
            "version": "unknown",
            "category": guess_category(name),
        }
        entries.append(entry)

    print(f"  [OK] mirror:{user_repo_tag} — imported {len(entries)} assets")
    return entries


def resolve_direct(url):
    """Pinned direct URL — used as-is."""
    filename = url.rstrip("/").split("/")[-1]
    name = re.sub(r"[_\-]v?\d.*$", "", filename.rsplit(".", 1)[0]) or filename
    entry = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": url,
        "source_direct": url,
        "description": f"{name} payload (pinned)",
        "last_update": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "version": "pinned",
        "category": guess_category(name),
    }
    return [entry]


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_links(path):
    lines = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
    except FileNotFoundError:
        print(f"ERROR: {path} not found")
        sys.exit(1)
    return lines


def main():
    lines = parse_links(LINKS_FILE)
    if not lines:
        print(f"ERROR: {LINKS_FILE} is empty")
        sys.exit(1)

    all_entries = []
    seen_urls = set()

    for line in lines:
        print(f"Processing: {line}")
        if line.startswith("github:"):
            entries = resolve_github(line[7:])
        elif line.startswith("mirror:"):
            entries = resolve_mirror(line[7:])
        elif line.startswith("http://") or line.startswith("https://"):
            entries = resolve_direct(line)
        else:
            print(f"  [WARN] Unknown format, skipping: {line}")
            continue

        for entry in entries:
            url = entry.get("url", "")
            if url in seen_urls:
                print(f"  [SKIP] Duplicate URL: {url}")
                continue
            seen_urls.add(url)
            all_entries.append(entry)

    if not all_entries:
        print("ERROR: All entries failed to resolve")
        sys.exit(2)

    # Validate URLs
    print(f"\nValidating {len(all_entries)} URLs...")
    valid_entries = []
    for entry in all_entries:
        url = entry.get("url", "")
        if head_ok(url):
            valid_entries.append(entry)
        else:
            print(f"  [SKIP] URL unreachable: {url}")

    if not valid_entries:
        print("ERROR: All URLs failed validation")
        sys.exit(2)

    # Write output
    new_json = json.dumps(valid_entries, indent=2, ensure_ascii=False)

    # Only write if changed
    try:
        with open(OUTPUT_FILE) as f:
            old_json = f.read()
        if old_json.strip() == new_json.strip():
            print(f"\nNo changes detected — {OUTPUT_FILE} not updated")
            sys.exit(0)
    except FileNotFoundError:
        pass

    with open(OUTPUT_FILE, "w") as f:
        f.write(new_json + "\n")

    print(f"\nWrote {len(valid_entries)} payloads to {OUTPUT_FILE}")
    sys.exit(0)


if __name__ == "__main__":
    main()