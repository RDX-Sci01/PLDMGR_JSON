#!/usr/bin/env python3
"""
Reads links.txt and writes payloads.json.

Two line formats:
  github:<user>/<repo>   — auto-resolves the latest release asset (.elf/.bin/.lua)
  https://...            — used as-is (pinned direct URL)

Run locally:  python3 generate.py
GitHub Actions runs this automatically on every push to links.txt.
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


def gh_api(path):
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def resolve_github(repo_path):
    """Return a payload dict from the latest release of <user>/<repo>."""
    print(f"  Fetching latest release for {repo_path} ...")
    try:
        data = gh_api(f"/repos/{repo_path}/releases/latest")
    except urllib.error.HTTPError as e:
        print(f"  WARNING: GitHub API error for {repo_path}: {e}", file=sys.stderr)
        return None

    tag = data.get("tag_name", "")
    assets = data.get("assets", [])

    # Pick the first asset with a supported extension
    asset = next(
        (a for a in assets if a["name"].lower().endswith(PAYLOAD_EXTENSIONS)),
        None,
    )

    if not asset:
        print(f"  WARNING: No .elf/.bin/.lua asset found in latest release of {repo_path}", file=sys.stderr)
        return None

    filename = asset["name"]
    url = asset["browser_download_url"]
    description = (data.get("body") or "").strip().splitlines()
    short_desc = description[0].lstrip("#").strip() if description else ""
    # Strip markdown bold/italic
    short_desc = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", short_desc)
    # Truncate long descriptions
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


def parse_filename(url):
    return url.rstrip("/").split("/")[-1].split("?")[0]


def infer_name(filename):
    stem = re.sub(r"\.[^.]+$", "", filename)
    stem = re.sub(r"[_\-]", " ", stem)
    stem = re.sub(r"\s+v?\d[\d.]*\w*$", "", stem, flags=re.IGNORECASE).strip()
    return stem.title()


def infer_version(filename):
    match = re.search(r"[_\-v](\d[\d.]+\w*)", filename, re.IGNORECASE)
    if match:
        return "v" + match.group(1).lstrip("vV")
    return None


def main():
    payloads = []

    with open(LINKS_FILE, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("github:"):
                repo_path = line[len("github:"):]
                entry = resolve_github(repo_path)
                if entry:
                    payloads.append(entry)
            else:
                # Direct URL
                filename = parse_filename(line)
                name = infer_name(filename)
                version = infer_version(filename)
                entry = {"name": name, "filename": filename, "url": line}
                if version:
                    entry["version"] = version
                payloads.append(entry)

    output = {"name": REPO_DISPLAY_NAME, "payloads": payloads}
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Written {len(payloads)} payload(s) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()