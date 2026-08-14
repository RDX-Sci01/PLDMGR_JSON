#!/usr/bin/env python3
"""
sync_links.py - auto-discovers payload repositories from itsPLK README
and updates links.txt.

Rules:
  - The configured mirror line is always preserved.
  - GitHub repositories discovered from the upstream README are automatic.
  - GitHub entries marked with "# MANUAL" are never removed.
  - Direct http/https URLs are always treated as pinned/manual entries
    and are never removed.
  - Repositories removed from the upstream README are removed unless
    marked "# MANUAL".
  - Output is deterministic.
  - links.txt is only rewritten when its contents change.
  - README content is downloaded directly rather than Base64-decoded.
  - GitHub API authentication is supported through GITHUB_TOKEN.
  - HTTP/API failures produce clear errors.

Exit codes:
  0 — success
  1 — failed to fetch/parse/write links.txt
"""

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

MIRROR_LINE = (
    "mirror:itsPLK/ps5-payloads-mirror@payloads-mirror"
)

USER_AGENT = "pldmgr-links-sync/1.1"
REQUEST_TIMEOUT = 15


# Repositories that should never be imported from the upstream README.
EXCLUDE_REPOS = {
    "itsPLK/ps5-payloads-mirror",
}


# Valid GitHub repository format:
#   owner/repository
REPO_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)


# Extract repository portion from GitHub URLs.
#
# Examples:
#   https://github.com/user/repo
#   https://github.com/user/repo/
#   https://github.com/user/repo/releases
#   https://github.com/user/repo/tree/main
GITHUB_REPO_URL_RE = re.compile(
    r"https?://github\.com/"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:[/?#\s]|$)",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def gh_headers(
    accept="application/vnd.github+json",
):
    """
    Return HTTP headers used for GitHub requests.
    """
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = (
            f"Bearer {GITHUB_TOKEN}"
        )

    return headers


def fetch_json(url):
    """
    Fetch and decode a GitHub API JSON response.
    """
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
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError(
                "GitHub API returned HTTP 403 "
                "(rate limit or permission denied)"
            ) from exc

        if exc.code == 404:
            raise RuntimeError(
                "GitHub API returned HTTP 404 "
                "(repository or README not found)"
            ) from exc

        raise RuntimeError(
            f"GitHub API returned HTTP "
            f"{exc.code} {exc.reason}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"connection error: {exc.reason}"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "request timed out"
        ) from exc

    except OSError as exc:
        raise RuntimeError(
            f"network error: {exc}"
        ) from exc

    try:
        return json.loads(raw)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON response: {exc}"
        ) from exc


def fetch_text(url):
    """
    Fetch plain text from a URL.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            return response.read().decode(
                "utf-8",
                errors="replace",
            )

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

    except OSError as exc:
        raise RuntimeError(
            f"network error: {exc}"
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# README handling
# ─────────────────────────────────────────────────────────────────────────────

def get_readme_content():
    """
    Fetch the upstream README.

    GitHub's Contents API provides a download_url for the README.
    We intentionally use that URL instead of decoding the API's
    Base64 'content' field.

    This avoids failures caused by whitespace/newline formatting
    in Base64 API responses.
    """
    api_url = (
        f"{GITHUB_API}/repos/"
        f"{README_REPO}/readme"
    )

    data = fetch_json(api_url)

    if not isinstance(data, dict):
        raise RuntimeError(
            "GitHub API returned an unexpected README response"
        )

    download_url = data.get("download_url")

    if download_url:
        download_url = str(
            download_url
        ).strip()

        if not (
            download_url.startswith("https://")
            or download_url.startswith("http://")
        ):
            raise RuntimeError(
                "GitHub returned an invalid README download URL"
            )

        return fetch_text(download_url)

    # Defensive fallback.
    #
    # Normally download_url is present. If GitHub ever omits it,
    # use the API content while tolerating normal Base64 whitespace.
    content = data.get("content")

    if not content:
        raise RuntimeError(
            "README response does not contain "
            "download_url or content"
        )

    encoding = str(
        data.get("encoding") or ""
    ).strip().lower()

    if encoding != "base64":
        return str(content)

    try:
        import base64

        normalized = re.sub(
            r"\s+",
            "",
            str(content),
        )

        decoded = base64.b64decode(
            normalized,
            validate=False,
        )

        return decoded.decode(
            "utf-8",
            errors="replace",
        )

    except Exception as exc:
        raise RuntimeError(
            f"could not decode README fallback: {exc}"
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Repository handling
# ─────────────────────────────────────────────────────────────────────────────

def normalize_repo(repo):
    """
    Validate and normalize a GitHub repository identifier.
    """
    if not isinstance(repo, str):
        return None

    repo = repo.strip().strip("/")

    if not REPO_RE.fullmatch(repo):
        return None

    owner, name = repo.split("/", 1)

    return f"{owner}/{name}"


def repo_key(repo):
    """
    Return a case-insensitive repository key.
    """
    return repo.casefold()


def is_excluded_repo(repo):
    """
    Return True when the repository is explicitly excluded.
    """
    if not repo:
        return False

    key = repo_key(repo)

    return any(
        key == repo_key(excluded)
        for excluded in EXCLUDE_REPOS
    )


def extract_github_repos(text):
    """
    Extract unique GitHub repositories from the upstream README.

    Only the owner/repository portion is retained.
    """
    if not isinstance(text, str):
        return []

    seen = set()
    repos = []

    for match in GITHUB_REPO_URL_RE.finditer(text):
        repo = normalize_repo(
            match.group(1)
        )

        if not repo:
            continue

        key = repo_key(repo)

        if key in seen:
            continue

        if is_excluded_repo(repo):
            continue

        seen.add(key)
        repos.append(repo)

    repos.sort(
        key=str.casefold
    )

    return repos


# ─────────────────────────────────────────────────────────────────────────────
# Existing links parsing
# ─────────────────────────────────────────────────────────────────────────────

def strip_inline_comment(value):
    """
    Remove a trailing inline comment.

    Example:

        github:user/repo # MANUAL

    becomes:

        github:user/repo
    """
    return re.sub(
        r"\s+#.*$",
        "",
        value,
    ).strip()


def has_manual_marker(line):
    """
    Return True when a line explicitly contains # MANUAL.
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
        automatic GitHub repositories,
        manual GitHub repositories,
        pinned direct URLs
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

        # Ignore blank lines and full-line comments.
        if not line or line.startswith("#"):
            continue

        is_manual = has_manual_marker(line)

        value = strip_inline_comment(
            line
        )

        if not value:
            continue

        # ── Mirror ─────────────────────────────────────────────────────

        if value.startswith("mirror:"):
            if mirror is None:
                mirror = value

            continue

        # ── GitHub repository ─────────────────────────────────────────

        if value.startswith("github:"):
            repo = normalize_repo(
                value[len("github:"):].strip()
            )

            if not repo:
                print(
                    "[WARN] Ignoring invalid GitHub entry: "
                    f"{value}"
                )
                continue

            key = repo_key(repo)

            if is_manual:
                manual_repos[key] = repo

                # A manual entry takes precedence over an automatic
                # entry for the same repository.
                auto_repos.pop(
                    key,
                    None,
                )

            elif key not in manual_repos:
                auto_repos[key] = repo

            continue

        # ── Direct URL ─────────────────────────────────────────────────

        #
        # Direct URLs are ALWAYS treated as pinned/manual entries.
        #
        if (
            value.startswith("http://")
            or value.startswith("https://")
        ):
            key = value.casefold()

            if key not in manual_urls:
                manual_urls[key] = value

            continue

        print(
            "[WARN] Ignoring unknown links.txt entry: "
            f"{value}"
        )

    return (
        mirror,
        auto_repos,
        manual_repos,
        manual_urls,
    )


# ─────────────────────────────────────────────────────────────────────────────
# links.txt generation
# ─────────────────────────────────────────────────────────────────────────────

def build_links_content(
    mirror,
    auto_repos,
    manual_repos,
    manual_urls,
):
    """
    Build deterministic links.txt content.

    NOTE:
    The sync timestamp is intentionally NOT included.

    This is important because otherwise links.txt would be rewritten
    on every GitHub Actions run even when nothing actually changed.
    """
    lines = [
        "# PS5 Payload Manager - Source List",
        "#",
        "# Formats:",
        "#   github:<user>/<repo>        - auto-resolves latest release",
        "#   mirror:<user>/<repo>@<tag>  - bulk-imports from a release tag",
        "#   https://...                 - pinned direct URL",
        "#",
        "# GitHub repositories discovered from the upstream README are",
        "# automatically added and removed as the README changes.",
        "#",
        "# Add # MANUAL to a GitHub repository to protect it from removal:",
        "#   github:myuser/myrepo # MANUAL",
        "#",
        "# Direct URLs are always treated as pinned/manual entries and",
        "# are preserved automatically.",
        "#",
        "# Example:",
        "#   https://example.com/payload.elf",
        "",
        "# --- Mirror ---",
        mirror or MIRROR_LINE,
        "",
        "# --- GitHub repos (auto-synced from itsPLK README) ---",
    ]

    # Automatic GitHub repositories.
    for repo in sorted(
        auto_repos.values(),
        key=str.casefold,
    ):
        lines.append(
            f"github:{repo}"
        )

    # Protected/manual GitHub repositories.
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

    # Pinned direct URLs.
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
            lines.append(url)

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# File handling
# ─────────────────────────────────────────────────────────────────────────────

def read_existing_file(path):
    """
    Read an existing file or return None if it doesn't exist.
    """
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

    The temporary file is created in the same directory so
    os.replace() remains atomic.
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


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(
        f"Fetching README from {README_REPO}..."
    )

    # ── Fetch upstream README ───────────────────────────────────────────

    try:
        readme = get_readme_content()

    except Exception as exc:
        print(
            f"ERROR: Could not fetch README: {exc}"
        )
        sys.exit(1)

    if not readme.strip():
        print(
            "ERROR: README is empty"
        )
        sys.exit(1)

    # ── Discover repositories ───────────────────────────────────────────

    discovered = extract_github_repos(
        readme
    )

    print(
        f"Discovered {len(discovered)} repositories "
        f"from README"
    )

    # ── Parse existing links.txt ────────────────────────────────────────

    try:
        (
            existing_mirror,
            old_auto,
            manual_repos,
            manual_urls,
        ) = parse_existing_links(
            LINKS_FILE
        )

    except Exception as exc:
        print(
            f"ERROR: Could not parse "
            f"{LINKS_FILE}: {exc}"
        )
        sys.exit(1)

    # ── Report preserved manual repositories ────────────────────────────

    if manual_repos:
        print(
            f"Preserving {len(manual_repos)} "
            f"MANUAL GitHub repositories:"
        )

        for repo in sorted(
            manual_repos.values(),
            key=str.casefold,
        ):
            print(
                f"  github:{repo} # MANUAL"
            )

    # ── Report preserved direct URLs ────────────────────────────────────

    if manual_urls:
        print(
            f"Preserving {len(manual_urls)} "
            f"pinned direct URLs:"
        )

        for url in sorted(
            manual_urls.values(),
            key=str.casefold,
        ):
            print(
                f"  {url}"
            )

    # ── Build automatic repository dictionary ───────────────────────────

    new_auto = {}

    for repo in discovered:
        key = repo_key(repo)

        # Never automatically add an excluded repository.
        if is_excluded_repo(repo):
            continue

        # A MANUAL entry wins over an automatic entry.
        if key in manual_repos:
            continue

        new_auto[key] = repo

    # ── Generate complete links.txt ─────────────────────────────────────

    content = build_links_content(
        existing_mirror or MIRROR_LINE,
        new_auto,
        manual_repos,
        manual_urls,
    )

    old_content = read_existing_file(
        LINKS_FILE
    )

    # ── No changes ──────────────────────────────────────────────────────

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
            f"Pinned direct URLs: "
            f"{len(manual_urls)}"
        )

        sys.exit(0)

    # ── Write atomically ────────────────────────────────────────────────

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

    # ── Calculate changes ───────────────────────────────────────────────

    old_auto_keys = set(
        old_auto
    )

    new_auto_keys = set(
        new_auto
    )

    added = sorted(
        new_auto_keys - new_auto_keys.intersection(
            old_auto_keys
        )
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

        for key in added:
            print(
                f"  {new_auto[key]}"
            )

    if removed:
        print(
            "Removed:"
        )

        for key in removed:
            print(
                f"  {old_auto[key]}"
            )

    if not added and not removed:
        print(
            "Changes were formatting/content related only."
        )

    # ── Summary ─────────────────────────────────────────────────────────

    print(
        f"Automatic repositories: "
        f"{len(new_auto)}"
    )

    print(
        f"Manual repositories: "
        f"{len(manual_repos)}"
    )

    print(
        f"Pinned direct URLs: "
        f"{len(manual_urls)}"
    )

    sys.exit(0)


if __name__ == "__main__":
    main()