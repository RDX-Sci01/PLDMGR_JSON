#!/usr/bin/env python3
"""
sync_links.py - auto-discovers payload repositories from itsPLK README
and updates links.txt.

Rules:
  - The configured mirror line is always preserved at the top.
  - GitHub repos discovered from the upstream README are automatic.
  - Entries marked with "# MANUAL" are never removed or overwritten.
  - Manual direct URLs must also be marked "# MANUAL".
  - Repos removed from the upstream README are removed unless marked MANUAL.
  - Output is deterministic and only rewritten when content changes.

Exit codes:
  0 — success
  1 — failed to fetch/parse the upstream README
"""

import base64
import binascii
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

README_REPO = "itsPLK/ps5-payloads-mirror"
LINKS_FILE = "links.txt"

MIRROR_LINE = (
    "mirror:itsPLK/ps5-payloads-mirror@payloads-mirror"
)

USER_AGENT = "pldmgr-links-sync/1.0"

REQUEST_TIMEOUT = 15


# Repositories that must never be imported from the upstream README.
EXCLUDE_REPOS = {
    "itsPLK/ps5-payloads-mirror",
}


# GitHub repository syntax:
#
#   owner/repository
#
# GitHub allows letters, numbers, -, _, and .
REPO_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)


# Matches ordinary GitHub repository URLs.
#
# Examples:
#   https://github.com/user/repo
#   https://github.com/user/repo/
#   https://github.com/user/repo/releases
#   https://github.com/user/repo/tree/main
#
# Only the owner/repository portion is retained.
GITHUB_REPO_URL_RE = re.compile(
    r"https?://github\.com/"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:[/?#\s]|$)",
    re.IGNORECASE,
)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def gh_headers():
    """Return HTTP headers used for GitHub API requests."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = (
            f"Bearer {GITHUB_TOKEN}"
        )

    return headers


def fetch_json(url):
    """Fetch a GitHub API JSON response."""
    request = urllib.request.Request(
        url,
        headers=gh_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            raw = response.read().decode("utf-8")

    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code} {exc.reason}"
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
        return json.loads(raw)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON response: {exc}"
        ) from exc


# ── GitHub README handling ───────────────────────────────────────────────────

def get_readme_content():
    """
    Fetch and decode the repository README through the GitHub API.
    """
    url = (
        f"{GITHUB_API}/repos/{README_REPO}/readme"
    )

    data = fetch_json(url)

    if not isinstance(data, dict):
        raise RuntimeError(
            "GitHub API returned an unexpected README response"
        )

    encoded_content = data.get("content")

    if not encoded_content:
        raise RuntimeError(
            "README response does not contain content"
        )

    try:
        decoded = base64.b64decode(
            encoded_content,
            validate=True,
        )

    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(
            f"invalid base64 README content: {exc}"
        ) from exc

    return decoded.decode(
        "utf-8",
        errors="replace",
    )


# ── Repository extraction ────────────────────────────────────────────────────

def normalize_repo(repo):
    """Normalize a GitHub repository identifier."""
    if not isinstance(repo, str):
        return None

    repo = repo.strip()

    if not REPO_RE.fullmatch(repo):
        return None

    owner, name = repo.split("/", 1)

    return f"{owner}/{name}"


def is_excluded_repo(repo):
    """Return True if a repository is explicitly excluded."""
    repo_key = repo.casefold()

    return any(
        repo_key == excluded.casefold()
        for excluded in EXCLUDE_REPOS
    )


def extract_github_repos(text):
    """
    Extract unique GitHub repositories from README links.

    Only the owner/repository portion is retained, so links such as:

        https://github.com/user/repo/releases
        https://github.com/user/repo/tree/main

    resolve to:

        user/repo
    """
    if not isinstance(text, str):
        return []

    seen = set()
    repos = []

    for match in GITHUB_REPO_URL_RE.finditer(text):
        repo = normalize_repo(match.group(1))

        if not repo:
            continue

        key = repo.casefold()

        if key in seen:
            continue

        if is_excluded_repo(repo):
            continue

        seen.add(key)
        repos.append(repo)

    # Deterministic ordering.
    repos.sort(key=str.casefold)

    return repos


# ── Existing links parsing ────────────────────────────────────────────────────

def strip_inline_comment(value):
    """
    Remove a trailing '#' comment while preserving the actual value.
    """
    return re.sub(
        r"\s+#.*$",
        "",
        value,
    ).strip()


def has_manual_marker(line):
    """
    Detect the explicit MANUAL protection marker.

    The marker must appear as a comment token, rather than matching
    arbitrary text containing the word MANUAL.
    """
    return bool(
        re.search(
            r"\s+#\s*MANUAL\b",
            line,
            flags=re.IGNORECASE,
        )
    )


def parse_existing_links(path):
    """
    Parse the existing links.txt.

    Returns:
        mirror,
        automatic repositories,
        manual repositories,
        manual direct URLs
    """
    mirror = None
    auto_repos = {}
    manual_repos = {}
    manual_urls = {}

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            lines = file.readlines()

    except FileNotFoundError:
        return None, {}, {}, {}

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

        # Mirror
        if value.startswith("mirror:"):
            if mirror is None:
                mirror = value

            continue

        # GitHub repository
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

            key = repo.casefold()

            if is_manual:
                if key not in manual_repos:
                    manual_repos[key] = repo
            else:
                if key not in auto_repos:
                    auto_repos[key] = repo

            continue

        # Direct URL
        if (
            value.startswith("http://")
            or value.startswith("https://")
        ):
            if not is_manual:
                print(
                    f"[WARN] Ignoring unprotected direct URL "
                    f"from existing links.txt: {value}"
                )
                continue

            key = value.casefold()

            if key not in manual_urls:
                manual_urls[key] = value

            continue

        print(
            f"[WARN] Ignoring unknown links.txt entry: "
            f"{value}"
        )

    return (
        mirror,
        auto_repos,
        manual_repos,
        manual_urls,
    )


# ── Output generation ────────────────────────────────────────────────────────

def build_links_content(
    mirror,
    auto_repos,
    manual_repos,
    manual_urls,
):
    """
    Build deterministic links.txt content.
    """
    lines = [
        "# PS5 Payload Manager - Source List",
        "#",
        "# Formats:",
        "#   github:<user>/<repo>        - auto-resolves latest release",
        "#   mirror:<user>/<repo>@<tag>  - bulk-imports from a release tag",
        "#   https://...                 - pinned direct URL",
        "#",
        "# Add # MANUAL to any GitHub or direct URL entry to protect it",
        "# from automatic synchronization.",
        "#",
        "# Examples:",
        "#   github:myuser/myrepo # MANUAL",
        "#   https://example.com/payload.elf # MANUAL",
        "",
        "# --- Mirror ---",
        mirror or MIRROR_LINE,
        "",
        "# --- GitHub repos (auto-synced from itsPLK README) ---",
        "# Last synced: "
        + datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
    ]

    for repo in sorted(
        auto_repos.values(),
        key=str.casefold,
    ):
        lines.append(
            f"github:{repo}"
        )

    if manual_repos:
        lines.extend(
            [
                "",
                "# --- Manually added GitHub repos ---",
            ]
        )

        for repo in sorted(
            manual_repos.values(),
            key=str.casefold,
        ):
            lines.append(
                f"github:{repo} # MANUAL"
            )

    if manual_urls:
        lines.extend(
            [
                "",
                "# --- Pinned direct URLs ---",
            ]
        )

        for url in sorted(
            manual_urls.values(),
            key=str.casefold,
        ):
            lines.append(
                f"{url} # MANUAL"
            )

    return "\n".join(lines) + "\n"


def read_existing_file(path):
    """Read a file if it exists."""
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
    """
    Atomically replace a file.

    The temporary file is created in the same directory so that
    os.replace() remains atomic on the same filesystem.
    """
    directory = os.path.dirname(
        os.path.abspath(path)
    )

    basename = os.path.basename(path)

    fd = None
    temp_path = None

    try:
        fd, temp_path = tempfile.mkstemp(
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
            temp_path,
            path,
        )

        temp_path = None

    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        raise

    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
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
        f"Discovered {len(discovered)} repositories "
        f"from README"
    )

    try:
        (
            existing_mirror,
            old_auto,
            manual_repos,
            manual_urls,
        ) = parse_existing_links(LINKS_FILE)

    except Exception as exc:
        print(
            f"ERROR: Could not parse {LINKS_FILE}: {exc}"
        )
        sys.exit(1)

    if manual_repos:
        print(
            f"Preserving {len(manual_repos)} "
            f"MANUAL GitHub entries:"
        )

        for repo in sorted(
            manual_repos.values(),
            key=str.casefold,
        ):
            print(
                f"  github:{repo} # MANUAL"
            )

    if manual_urls:
        print(
            f"Preserving {len(manual_urls)} "
            f"MANUAL direct URLs:"
        )

        for url in sorted(
            manual_urls.values(),
            key=str.casefold,
        ):
            print(
                f"  {url} # MANUAL"
            )

    # Build automatic repository set.
    new_auto = {
        repo.casefold(): repo
        for repo in discovered
    }

    # Never allow excluded repositories through, even if the exclusion
    # configuration changes case.
    for repo_key in list(new_auto):
        repo = new_auto[repo_key]

        if is_excluded_repo(repo):
            del new_auto[repo_key]

    content = build_links_content(
        existing_mirror or MIRROR_LINE,
        new_auto,
        manual_repos,
        manual_urls,
    )

    old_content = read_existing_file(
        LINKS_FILE
    )

    if old_content == content:
        print(
            "links.txt unchanged"
        )

        print(
            f"Automatic repositories: "
            f"{len(new_auto)}"
        )
        print(
            f"Manual repositories: "
            f"{len(manual_repos)}"
        )
        print(
            f"Manual URLs: "
            f"{len(manual_urls)}"
        )

        sys.exit(0)

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

    old_auto_keys = set(old_auto)
    new_auto_keys = set(new_auto)

    added = sorted(
        new_auto_keys - old_auto_keys
    )

    removed = sorted(
        old_auto_keys - new_auto_keys
    )

    print(
        f"Updated {LINKS_FILE}"
    )

    if added:
        print(
            "Added:"
        )

        for repo in added:
            print(
                f"  {new_auto[repo]}"
            )

    if removed:
        print(
            "Removed:"
        )

        for repo in removed:
            print(
                f"  {old_auto[repo]}"
            )

    if not added and not removed:
        print(
            "Changes were formatting/content related "
            "only."
        )

    print(
        f"Automatic repositories: "
        f"{len(new_auto)}"
    )
    print(
        f"Manual repositories: "
        f"{len(manual_repos)}"
    )
    print(
        f"Manual URLs: "
        f"{len(manual_urls)}"
    )

    sys.exit(0)


if __name__ == "__main__":
    main()