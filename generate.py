#!/usr/bin/env python3
"""
Reads links.txt and writes payloads.json.
Run locally: python3 generate.py
Or via GitHub Actions on every push to links.txt.
"""

import json
import re
import os

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"
REPO_NAME = os.environ.get("REPO_DISPLAY_NAME", "RDX Custom Payloads")

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
        v = match.group(1).lstrip("vV")
        return f"v{v}"
    return None

def main():
    payloads = []
    with open(LINKS_FILE, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            filename = parse_filename(line)
            name = infer_name(filename)
            version = infer_version(filename)
            entry = {"name": name, "filename": filename, "url": line}
            if version:
                entry["version"] = version
            payloads.append(entry)

    output = {"name": REPO_NAME, "payloads": payloads}
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Written {len(payloads)} payload(s) to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
