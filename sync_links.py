#!/usr/bin/env python3
"""
sync_links.py — auto-discovers payload repos from itsPLK's README and updates links.txt

What it does:
  1. Fetches https://github.com/itsPLK/ps5-payloads-mirror README via GitHub API
  2. Parses every github.com/<user>/<repo> link found in it
  3. Merges them into links.txt (preserving manual entries, adding new ones, removing dead ones)
  4. Always keeps the mirror: line at the top

Exit codes:
  0 — links.txt written (or unchanged)
  1 — could not fetch README
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

GITHUB_API       = "https://api.github.com"
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
README_REPO      = "itsPLK/ps5-payloads-mirror"
LINKS_FILE       = "links.txt"

# The mirror line always stays at the top
MIRROR_LINE      = "mirror:itsPLK/ps5-payloads-mirror@payloads-mirror"

# Repos to always exclude (forks, meta-repos, the mirror itself, etc.)
EXCLUDE_REPOS    = {
    "itsPLK/ps5-payloads-mirror",
    "itsPLK/pldmgr",          # payload manager itself, not a payload
}


def gh_headers():
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def fetch_text(url):
    req = urllib.request.Request(url, headers=gh_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def fetch_json(url):
    return json.loads(fetch_text(url))


def get_readme_content():
    """Fetch the raw README from itsPLK/ps5-payloads-mirror via GitHub API."""
    url = f"{GITHUB_API}/repos/{README_REPO}/readme"
    data = fetch_json(url)
    # Content is base64-encoded
    import base64
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def extract_github_repos(text):
    """
    Find all github.com/<user>/<repo> links in the README.
    Returns an ordered list of 'user/repo' strings (de-duplicated, preserving order).
    """
    # Match both markdown links and bare URLs
    pattern = re.compile(
        r'https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)',
        re.IGNORECASE
    )
    seen = set()
    repos = []
    for m in pattern.finditer(text):
        repo = m.group(1).rstrip("/. ")
        # Strip trailing path segments (e.g. /releases, /blob/main/...)
        parts = repo.split("/")
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
        if repo.lower() in {r.lower() for r in seen}:
            continue
        seen.add(repo)
        repos.append(repo)
    return repos


def parse_existing_links(path):
    """
    Reads links.txt and extracts:
      mirror_line    — first mirror: line found (or None)
      manual_entries — ordered list of direct https:// URLs (deduplicated)
      github_entries — ordered list of 'user/repo' strings (deduplicated)
    Skips comments, blanks, and duplicate entries regardless of how
    many times they appear or how many header blocks exist.
    """
    mirror = None
    manual_entries = {}   # lower -> original
    github_entries = {}   # lower -> original

    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None, {}, {}

    for line in lines:
        bare = line.strip()

        if not bare or bare.startswith("#"):
            continue

        if bare.startswith("mirror:") and mirror is None:
            mirror = bare
            continue

        if bare.startswith("mirror:"):
            continue  # deduplicate extra mirror lines

        if bare.startswith("github:"):
            repo = bare[7:].strip()
            if repo.lower() not in github_entries:
                github_entries[repo.lower()] = repo
            continue

        if bare.startswith("http://") or bare.startswith("https://"):
            if bare.lower() not in manual_entries:
                manual_entries[bare.lower()] = bare
            continue

    return mirror, manual_entries, github_entries


def write_links(path, mirror, github_repos, manual_entries):
    """Always writes a clean, canonical links.txt — fully overwrites the file."""
    lines = [
        "# PS5 Payload Manager - Source List",
        "# Formats:",
        "#   github:<user>/<repo>        — auto-resolves latest release, updates automatically",
        "#   mirror:<user>/<repo>@<tag>  — bulk-imports all .elf/.bin/.lua from a release tag",
        "#   https://...                 — pinned direct URL, will NOT auto-update",
        "#",
        "# Lines starting with # are ignored.",
        "",
        "# --- Mirror (bulk import from itsPLK's mirror) ---",
        mirror or MIRROR_LINE,
        "",
        "# --- GitHub repos (auto-synced from itsPLK README) ---",
        f"# Last synced: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]

    for repo in github_repos:
        lines.append(f"github:{repo}")

    if manual_entries:
        lines += [
            "",
            "# --- Pinned direct URLs (manual, will NOT auto-update) ---",
        ]
        for entry in manual_entries.values():
            lines.append(entry)

    new_content = "\n".join(lines) + "\n"

    # Only write if changed
    try:
        with open(path) as f:
            old_content = f.read()
        if old_content == new_content:
            print("links.txt unchanged — nothing to write")
            return False
    except FileNotFoundError:
        pass

    with open(path, "w") as f:
        f.write(new_content)

    return True


def main():
    print(f"Fetching README from {README_REPO}...")
    try:
        readme = get_readme_content()
    except Exception as e:
        print(f"ERROR: Could not fetch README: {e}")
        sys.exit(1)

    discovered = extract_github_repos(readme)
    print(f"Found {len(discovered)} GitHub repos in README")

    # Filter out excluded repos
    filtered = [r for r in discovered if r not in EXCLUDE_REPOS and not any(
        r.lower() == ex.lower() for ex in EXCLUDE_REPOS
    )]
    print(f"After exclusions: {len(filtered)} repos")
    for r in filtered:
        print(f"  github:{r}")

    # Load existing links.txt to preserve mirror line + manual pinned URLs
    mirror, manual_entries, old_github = parse_existing_links(LINKS_FILE)

    # Keep any manually-added github: entries not found in the README
    extra_manual_github = {
        k: v for k, v in old_github.items()
        if k not in {r.lower() for r in filtered}
    }
    if extra_manual_github:
        print(f"Preserving {len(extra_manual_github)} manual github: entries not in README:")
        for v in extra_manual_github.values():
            print(f"  github:{v}")

    final_github = filtered + list(extra_manual_github.values())

    changed = write_links(LINKS_FILE, mirror or MIRROR_LINE, final_github, manual_entries)
    if changed:
        print(f"links.txt updated with {len(final_github)} github: entries")
    sys.exit(0)


if __name__ == "__main__":
    main()