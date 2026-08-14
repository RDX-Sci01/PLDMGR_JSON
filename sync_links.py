#!/usr/bin/env python3
"""
sync_links.py — synchronize links.txt from the itsPLK README.

Rules:
  - GitHub repositories found in the upstream README are automatic.
  - GitHub entries marked "# MANUAL" are never removed.
  - Direct http/https URLs are always preserved.
  - No mirror entries are used.
  - Output is deterministic.
  - links.txt is only rewritten when its contents change.

Exit codes:
  0 — success
  1 — fetch/parse/write failure
"""

import base64
import binascii
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

README_REPO = "itsPLK/ps5-payloads-mirror"
LINKS_FILE = "links.txt"

USER_AGENT = "pldmgr-links-sync/2.0"
REQUEST_TIMEOUT = 20

# Repositories that must never be automatically imported.
EXCLUDE_REPOS = {
    "itsPLK/ps5-payloads-mirror",
}


# ─────────────────────────────────────────────────────────────────────────────
# Patterns
# ─────────────────────────────────────────────────────────────────────────────

REPO_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)

GITHUB_REPO_URL_RE = re.compile(
    r"https?://github\.com/"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:[/?#\s]|$)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API
# ─────────────────────────────────────────────────────────────────────────────

def github_headers():
    """Return standard GitHub API headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def fetch_json(url):
    """Fetch and decode a JSON response."""
    request = urllib.request.Request(
        url,
        headers=github_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            raw = response.read()

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        try:
            data = json.loads(body)
            message = data.get("message") or exc.reason
        except Exception:
            message = exc.reason

        raise RuntimeError(
            f"GitHub API HTTP {exc.code}: {message}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"connection error: {exc.reason}"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "request timed out"
        ) from exc

    try:
        return json.loads(raw.decode("utf-8"))

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"invalid JSON response: {exc}"
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# README
# ─────────────────────────────────────────────────────────────────────────────

def get_readme_content():
    """Fetch and decode the upstream README."""
    url = f"{GITHUB_API}/repos/{README_REPO}/readme"

    data = fetch_json(url)

    if not isinstance(data, dict):
        raise RuntimeError(
            "unexpected README API response"
        )

    encoded = data.get("content")

    if not encoded:
        raise RuntimeError(
            "README API response contains no content"
        )

    # GitHub may wrap Base64 content with newlines.
    encoded = re.sub(r"\s+", "", str(encoded))

    try:
        decoded = base64.b64decode(
            encoded,
            validate=True,
        )

    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(
            f"invalid Base64 README content: {exc}"
        ) from exc

    return decoded.decode(
        "utf-8",
        errors="replace",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Repository handling
# ─────────────────────────────────────────────────────────────────────────────

def normalize_repo(repo):
    """Validate and normalize owner/repository."""
    if not isinstance(repo, str):
        return None

    repo = repo.strip().strip("/")

    if not REPO_RE.fullmatch(repo):
        return None

    owner, name = repo.split("/", 1)

    return f"{owner}/{name}"


def repo_key(repo):
    """Return a case-insensitive repository key."""
    return repo.casefold()


def is_excluded_repo(repo):
    """Return True if repository is explicitly excluded."""
    key = repo_key(repo)

    return any(
        key == repo_key(excluded)
        for excluded in EXCLUDE_REPOS
    )


def extract_github_repos(readme):
    """Extract unique GitHub repositories from the README."""
    if not isinstance(readme, str):
        return []

    found = {}
    
    for match in GITHUB_REPO_URL_RE.finditer(readme):
        repo = normalize_repo(match.group(1))

        if not repo:
            continue

        if is_excluded_repo(repo):
            continue

        found[repo_key(repo)] = repo

    return sorted(
        found.values(),
        key=str.casefold,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Existing links.txt
# ─────────────────────────────────────────────────────────────────────────────

def has_manual_marker(line):
    """Return True when a line contains # MANUAL."""
    return bool(
        re.search(
            r"\s+#\s*MANUAL\b",
            line,
            flags=re.IGNORECASE,
        )
    )


def strip_inline_comment(value):
    """Remove an inline comment."""
    return re.sub(
        r"\s+#.*$",
        "",
        value,
    ).strip()


def parse_existing_links(path):
    """
    Return:

        automatic GitHub repositories
        manual GitHub repositories
        pinned direct URLs
    """

    automatic = {}
    manual = {}
    direct_urls = {}

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            lines = file.readlines()

    except FileNotFoundError:
        return {}, {}, {}

    except OSError as exc:
        raise RuntimeError(
            f"could not read {path}: {exc}"
        ) from exc

    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        is_manual = has_manual_marker(line)
        value = strip_inline_comment(line)

        if not value:
            continue

        # GitHub repository.
        if value.startswith("github:"):
            repo = normalize_repo(
                value[len("github:"):].strip()
            )

            if not repo:
                print(
                    f"[WARN] Ignoring invalid GitHub entry: "
                    f"{value}"
                )
                continue

            key = repo_key(repo)

            if is_manual:
                manual[key] = repo
            else:
                automatic[key] = repo

            continue

        # Direct URL.
        if value.startswith(("http://", "https://")):
            direct_urls[value.casefold()] = value
            continue

        print(
            f"[WARN] Ignoring unknown entry: {value}"
        )

    return automatic, manual, direct_urls


# ─────────────────────────────────────────────────────────────────────────────
# links.txt generation
# ─────────────────────────────────────────────────────────────────────────────

def build_links_content(
    automatic,
    manual,
    direct_urls,
):
    """Build deterministic links.txt content."""

    lines = [
        "# PS5 Payload Manager - Source List",
        "#",
        "# Formats:",
        "#   github:<user>/<repo>  - auto-resolves latest release",
        "#   https://...           - pinned direct payload URL",
        "#",
        "# GitHub repositories discovered from the itsPLK README are",
        "# automatically added and removed as the upstream list changes.",
        "#",
        "# Add # MANUAL to a GitHub repository to protect it from removal:",
        "#   github:myuser/myrepo # MANUAL",
        "#",
        "# Direct URLs are always preserved.",
        "",
        "# --- GitHub repos (auto-synced from itsPLK README) ---",
        "# Last synced: "
        + datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
    ]

    for repo in sorted(
        automatic.values(),
        key=str.casefold,
    ):
        lines.append(
            f"github:{repo}"
        )

    if manual:
        lines.extend(
            [
                "",
                "# --- Manually added GitHub repos ---",
            ]
        )

        for repo in sorted(
            manual.values(),
            key=str.casefold,
        ):
            lines.append(
                f"github:{repo} # MANUAL"
            )

    if direct_urls:
        lines.extend(
            [
                "",
                "# --- Pinned direct URLs ---",
            ]
        )

        for url in sorted(
            direct_urls.values(),
            key=str.casefold,
        ):
            lines.append(url)

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# File handling
# ─────────────────────────────────────────────────────────────────────────────

def read_file(path):
    """Read a file or return None when it doesn't exist."""
    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            return file.read()

    except FileNotFoundError:
        return None

    except OSError as exc:
        raise RuntimeError(
            f"could not read {path}: {exc}"
        ) from exc


def atomic_write(path, content):
    """Atomically replace a file."""
    directory = os.path.dirname(
        os.path.abspath(path)
    )

    basename = os.path.basename(path)

    fd = None
    temporary = None

    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{basename}.",
            suffix=".tmp",
            dir=directory,
            text=True,
        )

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as file:
            fd = None
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temporary,
            path,
        )

        temporary = None

    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("PLDMGR links synchronizer")
    print("=" * 72)

    print(
        f"Fetching README from {README_REPO}..."
    )

    try:
        readme = get_readme_content()

    except Exception as exc:
        print(
            f"ERROR: Could not fetch README: {exc}"
        )
        sys.exit(1)

    discovered = extract_github_repos(readme)

    print(
        f"Discovered {len(discovered)} repositories"
    )

    try:
        old_automatic, manual, direct_urls = (
            parse_existing_links(LINKS_FILE)
        )

    except Exception as exc:
        print(
            f"ERROR: Could not parse "
            f"{LINKS_FILE}: {exc}"
        )
        sys.exit(1)

    # Never automatically import an excluded repository.
    automatic = {
        repo_key(repo): repo
        for repo in discovered
        if not is_excluded_repo(repo)
    }

    # Never allow a manual repository to accidentally appear
    # in both automatic and manual sections.
    for key in manual:
        automatic.pop(key, None)

    content = build_links_content(
        automatic,
        manual,
        direct_urls,
    )

    old_content = read_file(LINKS_FILE)

    if old_content == content:
        print("links.txt unchanged")
    else:
        try:
            atomic_write(
                LINKS_FILE,
                content,
            )

        except OSError as exc:
            print(
                f"ERROR: Could not write "
                f"{LINKS_FILE}: {exc}"
            )
            sys.exit(1)

        print("Updated links.txt")

        added = sorted(
            set(automatic) - set(old_automatic),
        )

        removed = sorted(
            set(old_automatic) - set(automatic),
        )

        if added:
            print("\nAdded:")
            for key in added:
                print(f"  {automatic[key]}")

        if removed:
            print("\nRemoved:")
            for key in removed:
                print(f"  {old_automatic[key]}")

    print()
    print(f"Automatic repositories: {len(automatic)}")
    print(f"Manual repositories:    {len(manual)}")
    print(f"Pinned direct URLs:     {len(direct_urls)}")
    print("=" * 72)

    sys.exit(0)


if __name__ == "__main__":
    main()