#!/usr/bin/env python3
"""
Reads links.txt and writes payloads.json.

Two line formats:
  github:<user>/<repo>   — auto-resolves the latest release asset (.elf/.bin/.lua)
  https://...            — used as-is (pinned direct URL)

Run locally:  python3 generate.py
GitHub Actions runs this automatically on schedule and on every push to links.txt.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"
REPO_DISPLAY_NAME = os.environ.get("REPO_DISPLAY_NAME", "RDX Custom Payloads")
PAYLOAD_EXTENSIONS = (".elf", ".bin", ".lua")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Exit codes
EXIT_OK = 0
EXIT_NO_PAYLOADS = 1
EXIT_ALL_INVALID = 2


# ─── GitHub API ───────────────────────────────────────────────────────────────

def gh_api(path):
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def resolve_mirror(mirror_spec):
    """
    Fetch ALL .elf/.bin/.lua assets from a pinned release tag.
    Format: mirror:<user>/<repo>@<tag>
    Returns a list of payload dicts (one per asset).
    """
    try:
        repo_path, tag = mirror_spec.rsplit("@", 1)
    except ValueError:
        print(f"  [mirror] WARNING: Invalid format '{mirror_spec}' — expected mirror:<user>/<repo>@<tag>", file=sys.stderr)
        return []

    print(f"  [mirror:{repo_path}@{tag}] Fetching release assets ...")
    try:
        data = gh_api(f"/repos/{repo_path}/releases/tags/{tag}")
    except urllib.error.URLError as e:
        print(f"  [mirror:{repo_path}@{tag}] WARNING: GitHub API unreachable: {e}", file=sys.stderr)
        return []

    assets = data.get("assets", [])
    payload_assets = [a for a in assets if a["name"].lower().endswith(PAYLOAD_EXTENSIONS)]

    if not payload_assets:
        print(f"  [mirror:{repo_path}@{tag}] WARNING: No .elf/.bin/.lua assets found.", file=sys.stderr)
        return []

    entries = []
    for asset in payload_assets:
        filename = asset["name"]
        url = asset["browser_download_url"]
        name = infer_name(filename)
        version = infer_version(filename)
        entry = {"name": name, "filename": filename, "url": url}
        if version:
            entry["version"] = version
        entries.append(entry)

    print(f"  [mirror:{repo_path}@{tag}] Found {len(entries)} payload(s).")
    return entries


def resolve_github(repo_path):
    """Return a payload dict from the latest release of <user>/<repo>, or None on failure."""
    print(f"  [{repo_path}] Fetching latest release ...")
    try:
        data = gh_api(f"/repos/{repo_path}/releases/latest")
    except urllib.error.URLError as e:
        # URLError is the parent of HTTPError — catches 4xx/5xx, timeouts, DNS failures
        print(f"  [{repo_path}] WARNING: GitHub API unreachable: {e}", file=sys.stderr)
        return None

    tag = data.get("tag_name", "")
    assets = data.get("assets", [])

    # Pick the first asset with a supported extension
    asset = next(
        (a for a in assets if a["name"].lower().endswith(PAYLOAD_EXTENSIONS)),
        None,
    )

    if not asset:
        print(f"  [{repo_path}] WARNING: No .elf/.bin/.lua asset in latest release.", file=sys.stderr)
        return None

    filename = asset["name"]
    url = asset["browser_download_url"]

    # Pull and clean the first non-empty line of the release body as description
    body_lines = [l.strip() for l in (data.get("body") or "").splitlines() if l.strip()]
    short_desc = body_lines[0].lstrip("#").strip() if body_lines else ""
    short_desc = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", short_desc)  # strip bold/italic
    if len(short_desc) > 120:
        short_desc = short_desc[:117].rstrip() + "..."

    name = infer_name(filename)
    version = tag if tag else infer_version(filename)

    entry = {"name": name, "filename": filename, "url": url}
    if version:
        entry["version"] = version
    if short_desc:
        entry["description"] = short_desc
    return entry


# ─── URL validation ───────────────────────────────────────────────────────────

def validate_url(url):
    """
    Send a HEAD request to confirm the file is actually downloadable.
    Falls back to a GET with stream if the server doesn't support HEAD.
    Returns (ok: bool, reason: str).
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("User-Agent", "PLDMGR_JSON-validator/1.0")
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                if 200 <= status < 300:
                    return True, f"HTTP {status}"
                return False, f"HTTP {status}"
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code == 405:
                # Server rejected HEAD — retry with GET
                continue
            return False, f"HTTP {e.code} {e.reason}"
        except urllib.error.URLError as e:
            return False, str(e.reason)
    return False, "HEAD and GET both failed"


# ─── Filename helpers ─────────────────────────────────────────────────────────

def parse_filename(url):
    # Strip query string and URL fragment before extracting filename
    return url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]


def infer_name(filename):
    stem = re.sub(r"\.[^.]+$", "", filename)            # drop extension
    stem = re.sub(r"[_\-]", " ", stem)                  # underscores/dashes → spaces
    stem = re.sub(r"\s+v?\d[\d.]*\w*$", "", stem, flags=re.IGNORECASE).strip()  # drop trailing version
    stem = re.sub(r"^v?\d[\d.]*\w*$", "", stem, flags=re.IGNORECASE).strip()    # entire stem is a version
    return stem.title() if stem else filename            # fall back to raw filename if nothing left


def infer_version(filename):
    # Strip extension first so "v0.19.elf" doesn't bleed ".elf" into the version string
    stem = re.sub(r"\.[^.]+$", "", filename)
    match = re.search(r"[_\-v](\d[\d.]*\w*)", stem, re.IGNORECASE)
    if match:
        return "v" + match.group(1).lstrip("vV")
    return None


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    payloads = []
    skipped = 0

    with open(LINKS_FILE, "r") as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

    if not lines:
        print("ERROR: links.txt is empty or has no active entries.", file=sys.stderr)
        sys.exit(EXIT_NO_PAYLOADS)

    for line in lines:
        if line.lower().startswith("mirror:"):
            # Bulk-import all payload assets from a pinned release tag
            mirror_spec = line[len("mirror:"):]
            entries = resolve_mirror(mirror_spec)
            if not entries:
                skipped += 1
                continue
            for entry in entries:
                url = entry["url"]
                print(f"  [{entry['name']}] Validating {url} ...")
                ok, reason = validate_url(url)
                if ok:
                    print(f"  [{entry['name']}] OK ({reason})")
                    payloads.append(entry)
                else:
                    print(f"  [{entry['name']}] SKIPPED — URL unreachable: {reason}", file=sys.stderr)
                    skipped += 1
            continue

        elif line.lower().startswith("github:"):
            repo_path = line[len("github:"):]
            entry = resolve_github(repo_path)
            if not entry:
                skipped += 1
                continue
        else:
            # Direct URL
            filename = parse_filename(line)
            name = infer_name(filename)
            version = infer_version(filename)
            entry = {"name": name, "filename": filename, "url": line}
            if version:
                entry["version"] = version

        # ── Validate the resolved download URL ──
        url = entry["url"]
        print(f"  [{entry['name']}] Validating {url} ...")
        ok, reason = validate_url(url)
        if ok:
            print(f"  [{entry['name']}] OK ({reason})")
            payloads.append(entry)
        else:
            print(f"  [{entry['name']}] SKIPPED — URL unreachable: {reason}", file=sys.stderr)
            skipped += 1

    print(f"\nResolved: {len(payloads)}  Skipped: {skipped}")

    if not payloads:
        print("ERROR: No valid payloads — payloads.json not written.", file=sys.stderr)
        sys.exit(EXIT_ALL_INVALID)

    output = {"name": REPO_DISPLAY_NAME, "payloads": payloads}
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Written {len(payloads)} payload(s) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()