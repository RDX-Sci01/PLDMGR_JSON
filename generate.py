#!/usr/bin/env python3
"""
generate.py — Generate payloads.json from manually maintained links.txt.

Supported links.txt entries:

    github:owner/repository # MANUAL
    https://example.com/payload.elf
    https://example.com/payload.bin
    https://example.com/payload.lua

GitHub repositories are resolved to their latest published release.

Only .elf, .bin and .lua release assets are included.

The generated payloads.json is deterministic and is only rewritten
when its contents actually change.

Exit codes:
    0 — success
    1 — links.txt missing/invalid or fatal input error
    2 — no usable payloads generated
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse


# =============================================================================
# Configuration
# =============================================================================

GITHUB_API = "https://api.github.com"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"

SUPPORTED_EXTENSIONS = (
    ".elf",
    ".bin",
    ".lua",
)

HTTP_TIMEOUT = 15

DESCRIPTION_MAX_LENGTH = 180

DEFAULT_CATEGORY = "Utilities & Tools"

USER_AGENT = "PLDMGR-JSON-Generator/2.0"


# =============================================================================
# HTTP helpers
# =============================================================================

def github_headers() -> Dict[str, str]:
    """Return standard GitHub API headers."""

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def fetch_json(url: str) -> object:
    """Fetch JSON from a URL."""

    request = urllib.request.Request(
        url,
        headers=github_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            data = response.read()

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
        return json.loads(
            data.decode("utf-8")
        )

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"invalid JSON response: {exc}"
        ) from exc


def http_ok(url: str) -> bool:
    """
    Validate a payload URL.

    Try HEAD first.

    Some CDNs reject HEAD, so fall back to a tiny GET request.
    """

    if not url:
        return False

    headers = {
        "User-Agent": USER_AGENT,
    }

    # ------------------------------------------------------------------
    # HEAD
    # ------------------------------------------------------------------

    try:
        request = urllib.request.Request(
            url,
            headers=headers,
            method="HEAD",
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return 200 <= response.status < 400

    except Exception:
        pass

    # ------------------------------------------------------------------
    # GET / Range fallback
    # ------------------------------------------------------------------

    try:
        request = urllib.request.Request(
            url,
            headers={
                **headers,
                "Range": "bytes=0-0",
            },
            method="GET",
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return 200 <= response.status < 400

    except Exception:
        return False


# =============================================================================
# Text helpers
# =============================================================================

def clean_description(
    text: str,
    fallback: str = "",
) -> str:
    """Convert Markdown/HTML into short plain text."""

    if not text:
        text = fallback

    if not text:
        return ""

    text = html.unescape(str(text))

    # HTML tags.
    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    # Markdown images.
    text = re.sub(
        r"!\[[^\]]*\]\([^)]*\)",
        " ",
        text,
    )

    # Markdown links.
    text = re.sub(
        r"\[([^\]]+)\]\([^)]*\)",
        r"\1",
        text,
    )

    # Inline code.
    text = re.sub(
        r"`([^`]*)`",
        r"\1",
        text,
    )

    # Markdown emphasis.
    text = re.sub(
        r"[*_~]+",
        "",
        text,
    )

    # Blockquotes.
    text = re.sub(
        r"^\s*[>#]+\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Headings.
    text = re.sub(
        r"^\s*#+\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Collapse whitespace.
    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    if len(text) > DESCRIPTION_MAX_LENGTH:
        text = text[:DESCRIPTION_MAX_LENGTH].rstrip()

        if " " in text:
            text = text.rsplit(" ", 1)[0]

        text += "..."

    return text


def release_description(release: dict) -> str:
    """Get the first useful line from release notes."""

    body = release.get("body") or ""

    for line in body.splitlines():
        cleaned = clean_description(line)

        if cleaned:
            return cleaned

    return ""


def date_from_iso(value: Optional[str]) -> str:
    """Convert an ISO timestamp into YYYY-MM-DD."""

    if value:
        match = re.match(
            r"^(\d{4}-\d{2}-\d{2})",
            str(value),
        )

        if match:
            return match.group(1)

    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")


# =============================================================================
# Payload helpers
# =============================================================================

def is_payload(filename: str) -> bool:
    """Return True for supported payload files."""

    if not filename:
        return False

    return filename.lower().endswith(
        SUPPORTED_EXTENSIONS
    )


def filename_without_extension(
    filename: str,
) -> str:
    """Return filename without its final extension."""

    return os.path.splitext(
        os.path.basename(filename)
    )[0]


def normalize_payload_name(
    filename: str,
) -> str:
    """
    Generate a clean payload name from an asset filename.

    Examples:

        elfldr-ps5.elf
            -> elfldr

        elfldr_v0.24.elf
            -> elfldr

        BFpilot_v0.4.4.elf
            -> BFpilot
    """

    name = filename_without_extension(
        filename
    )

    # Remove version suffixes.
    name = re.sub(
        r"([_\-\s]+)v?\d+(?:\.\d+)*"
        r"(?:[-_a-zA-Z0-9]*)?$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Remove PS4 / PS5 suffix.
    name = re.sub(
        r"[-_](?:ps4|ps5)$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Remove installer suffix.
    name = re.sub(
        r"[-_]installer$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    name = name.strip(
        "_- "
    )

    return (
        name
        or filename_without_extension(filename)
    )


def guess_category(
    name: str,
    description: str = "",
) -> str:
    """Assign a user-friendly payload category."""

    text = f"{name} {description}".lower()

    # ---------------------------------------------------------------
    # System / jailbreak
    # ---------------------------------------------------------------

    system_keywords = (
        "jailbreak",
        "kstuff",
        "etahen",
        "hen",
        "exploit",
        "lapy",
        "daemon",
    )

    if any(
        keyword in text
        for keyword in system_keywords
    ):
        return "System & Jailbreak"

    # ---------------------------------------------------------------
    # Loaders
    # ---------------------------------------------------------------

    loader_keywords = (
        "loader",
        "elfldr",
        "autoloader",
        "webkit",
        "elf loader",
    )

    if any(
        keyword in text
        for keyword in loader_keywords
    ):
        return "Loaders"

    # ---------------------------------------------------------------
    # Networking
    # ---------------------------------------------------------------

    networking_keywords = (
        "ftp",
        "ftpsrv",
        "http server",
        "web server",
        "websrv",
        "server",
        "srv",
        "dns",
        "upload",
        "network",
        "telnet",
        "shell server",
        "zftpd",
        "ps5upload",
    )

    if any(
        keyword in text
        for keyword in networking_keywords
    ):
        return "Networking & Servers"

    return DEFAULT_CATEGORY


# =============================================================================
# links.txt parsing
# =============================================================================

GITHUB_RE = re.compile(
    r"^github:([A-Za-z0-9_.-]+/"
    r"[A-Za-z0-9_.-]+)$",
    re.IGNORECASE,
)


def parse_links(
    path: str,
) -> tuple[List[str], List[str]]:
    """
    Parse links.txt.

    Returns:

        github_repositories
        direct_urls
    """

    github_repositories: List[str] = []
    direct_urls: List[str] = []

    seen_github = set()
    seen_urls = set()

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            lines = file.readlines()

    except FileNotFoundError:
        print(
            f"ERROR: {path} not found"
        )
        sys.exit(1)

    except OSError as exc:
        print(
            f"ERROR: Could not read {path}: {exc}"
        )
        sys.exit(1)

    for raw_line in lines:
        line = raw_line.strip()

        # Ignore blank lines and full comments.
        if not line or line.startswith("#"):
            continue

        # Remove inline comments.
        value = re.sub(
            r"\s+#.*$",
            "",
            line,
        ).strip()

        if not value:
            continue

        # -----------------------------------------------------------
        # GitHub repository
        # -----------------------------------------------------------

        match = GITHUB_RE.fullmatch(value)

        if match:
            repo = match.group(1)

            key = repo.casefold()

            if key not in seen_github:
                seen_github.add(key)
                github_repositories.append(repo)

            continue

        # -----------------------------------------------------------
        # Direct URL
        # -----------------------------------------------------------

        if (
            value.startswith("https://")
            or value.startswith("http://")
        ):
            key = value.casefold()

            if key not in seen_urls:
                seen_urls.add(key)
                direct_urls.append(value)

            continue

        print(
            f"  [WARN] Ignoring unknown entry: "
            f"{value}"
        )

    return (
        github_repositories,
        direct_urls,
    )


# =============================================================================
# GitHub resolver
# =============================================================================

def resolve_github(
    repo: str,
) -> List[dict]:
    """
    Resolve a GitHub repository to the
    latest published release.
    """

    repo = repo.strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/"
        r"[A-Za-z0-9_.-]+",
        repo,
    ):
        print(
            f"  [WARN] github:{repo} — "
            "invalid repository format"
        )
        return []

    url = (
        f"{GITHUB_API}/repos/"
        f"{repo}/releases/latest"
    )

    try:
        release = fetch_json(url)

    except urllib.error.HTTPError:
        return []

    except Exception as exc:
        print(
            f"  [WARN] github:{repo} — "
            f"{exc}"
        )
        return []

    if not isinstance(
        release,
        dict,
    ):
        print(
            f"  [WARN] github:{repo} — "
            "invalid release response"
        )
        return []

    assets = release.get(
        "assets"
    ) or []

    payload_assets = [
        asset
        for asset in assets
        if (
            isinstance(asset, dict)
            and is_payload(
                asset.get("name", "")
            )
            and asset.get(
                "browser_download_url"
            )
        )
    ]

    if not payload_assets:
        print(
            f"  [WARN] github:{repo} — "
            "latest release contains "
            "no supported payload assets"
        )
        return []

    tag = str(
        release.get("tag_name")
        or "unknown"
    ).strip()

    description = release_description(
        release
    )

    source = (
        f"https://github.com/"
        f"{repo}/releases"
    )

    entries = []

    for asset in payload_assets:
        filename = asset["name"]

        direct_url = (
            asset["browser_download_url"]
        )

        name = normalize_payload_name(
            filename
        )

        clean_desc = clean_description(
            description,
            fallback=f"{name} payload",
        )

        entries.append(
            {
                "name": name,
                "filename": filename,
                "url": direct_url,
                "source": source,
                "source_direct": direct_url,
                "description": (
                    clean_desc
                    or f"{name} payload"
                ),
                "last_update": date_from_iso(
                    release.get("published_at")
                    or release.get("created_at")
                ),
                "version": tag,
                "category": guess_category(
                    name,
                    clean_desc,
                ),
            }
        )

    print(
        f"  [OK] github:{repo} — "
        f"{len(entries)} payload(s), "
        f"{tag}"
    )

    return entries


# =============================================================================
# Direct URL resolver
# =============================================================================

def resolve_direct(
    url: str,
) -> List[dict]:
    """Create a payload entry from a direct URL."""

    parsed = urlparse(url)

    filename = unquote(
        os.path.basename(
            parsed.path
        )
    ).strip()

    if not filename:
        print(
            f"  [WARN] Direct URL has "
            f"no filename: {url}"
        )
        return []

    if not is_payload(filename):
        print(
            f"  [WARN] Unsupported direct "
            f"payload type: {filename}"
        )
        return []

    name = normalize_payload_name(
        filename
    )

    return [
        {
            "name": name,
            "filename": filename,
            "url": url,
            "source": url,
            "source_direct": url,
            "description": (
                f"{name} payload (pinned)"
            ),
            "last_update": datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%d"),
            "version": "pinned",
            "category": guess_category(name),
        }
    ]


# =============================================================================
# Validation
# =============================================================================

def validate_entries(
    entries: List[dict],
) -> List[dict]:
    """Validate all payload download URLs."""

    valid = []

    print(
        f"\nValidating {len(entries)} "
        f"payload URL(s)..."
    )

    for entry in entries:
        url = entry.get(
            "url",
            "",
        )

        name = entry.get(
            "name",
            "unknown",
        )

        filename = entry.get(
            "filename",
            "",
        )

        if http_ok(url):
            valid.append(entry)

            print(
                f"  [OK] {name} — "
                f"{filename}"
            )

        else:
            print(
                f"  [SKIP] URL unreachable: "
                f"{url}"
            )

    return valid


# =============================================================================
# Deduplication
# =============================================================================

def payload_identity(
    entry: dict,
) -> tuple:
    """Create a stable identity for a payload."""

    return (
        str(
            entry.get("url")
            or ""
        ).strip().lower(),
    )


def deduplicate(
    entries: List[dict],
) -> List[dict]:
    """Remove duplicate payload URLs."""

    unique = {}

    for entry in entries:
        identity = payload_identity(
            entry
        )

        if identity not in unique:
            unique[identity] = entry

    return list(
        unique.values()
    )


# =============================================================================
# Sorting
# =============================================================================

CATEGORY_ORDER = {
    "System & Jailbreak": 0,
    "Loaders": 1,
    "Networking & Servers": 2,
    "Utilities & Tools": 3,
}


def sort_entries(
    entries: List[dict],
) -> List[dict]:
    """Sort payloads deterministically."""

    return sorted(
        entries,
        key=lambda entry: (
            CATEGORY_ORDER.get(
                entry.get("category", ""),
                99,
            ),
            str(
                entry.get("name", "")
            ).casefold(),
            str(
                entry.get("version", "")
            ).casefold(),
            str(
                entry.get("filename", "")
            ).casefold(),
            str(
                entry.get("url", "")
            ).casefold(),
        ),
    )


# =============================================================================
# Output
# =============================================================================

def write_output(
    entries: List[dict],
) -> bool:
    """
    Write payloads.json only when contents changed.

    Returns True when the file changed.
    """

    public_entries = sort_entries(
        entries
    )

    new_json = (
        json.dumps(
            public_entries,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    try:
        with open(
            OUTPUT_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            old_json = file.read()

        if old_json == new_json:
            return False

    except FileNotFoundError:
        pass

    temporary_file = (
        OUTPUT_FILE + ".tmp"
    )

    try:
        with open(
            temporary_file,
            "w",
            encoding="utf-8",
        ) as file:
            file.write(new_json)
            file.flush()
            os.fsync(
                file.fileno()
            )

        os.replace(
            temporary_file,
            OUTPUT_FILE,
        )

    finally:
        if os.path.exists(
            temporary_file
        ):
            try:
                os.remove(
                    temporary_file
                )
            except OSError:
                pass

    return True


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    print(
        "PLDMGR payload generator"
    )
    print(
        "=" * 72
    )

    # -----------------------------------------------------------------
    # Parse links.txt
    # -----------------------------------------------------------------

    github_repositories, direct_urls = (
        parse_links(
            LINKS_FILE
        )
    )

    if (
        not github_repositories
        and not direct_urls
    ):
        print(
            f"ERROR: {LINKS_FILE} contains "
            "no usable sources"
        )
        sys.exit(1)

    print(
        f"\nGitHub repositories: "
        f"{len(github_repositories)}"
    )

    print(
        f"Direct URLs: "
        f"{len(direct_urls)}"
    )

    # -----------------------------------------------------------------
    # Resolve GitHub repositories
    # -----------------------------------------------------------------

    github_entries = []

    print(
        "\nResolving GitHub repositories..."
    )

    for repo in github_repositories:
        print(
            f"\nProcessing: github:{repo}"
        )

        github_entries.extend(
            resolve_github(repo)
        )

    # -----------------------------------------------------------------
    # Resolve direct URLs
    # -----------------------------------------------------------------

    direct_entries = []

    if direct_urls:
        print(
            "\nProcessing direct URLs..."
        )

    for url in direct_urls:
        print(
            f"\nProcessing: {url}"
        )

        direct_entries.extend(
            resolve_direct(url)
        )

    # -----------------------------------------------------------------
    # Combine
    # -----------------------------------------------------------------

    entries = (
        github_entries
        + direct_entries
    )

    entries = deduplicate(
        entries
    )

    print(
        "\n" + "=" * 72
    )

    print(
        f"Resolved payloads: "
        f"{len(entries)}"
    )

    if not entries:
        print(
            "ERROR: No payloads could "
            "be resolved"
        )
        sys.exit(2)

    # -----------------------------------------------------------------
    # Validate
    # -----------------------------------------------------------------

    entries = validate_entries(
        entries
    )

    if not entries:
        print(
            "\nERROR: All payload URLs "
            "failed validation"
        )
        sys.exit(2)

    # -----------------------------------------------------------------
    # Final deterministic ordering
    # -----------------------------------------------------------------

    entries = sort_entries(
        entries
    )

    # -----------------------------------------------------------------
    # Write output
    # -----------------------------------------------------------------

    changed = write_output(
        entries
    )

    print(
        "\n" + "=" * 72
    )

    if changed:
        print(
            f"Wrote {len(entries)} "
            f"payload(s) to "
            f"{OUTPUT_FILE}"
        )
    else:
        print(
            f"No changes detected — "
            f"{OUTPUT_FILE} unchanged"
        )

    # -----------------------------------------------------------------
    # Category summary
    # -----------------------------------------------------------------

    category_counts: Dict[str, int] = {}

    for entry in entries:
        category = entry.get(
            "category",
            DEFAULT_CATEGORY,
        )

        category_counts[category] = (
            category_counts.get(
                category,
                0,
            )
            + 1
        )

    print(
        "\nCatalog summary:"
    )

    for category in sorted(
        category_counts,
        key=lambda value: (
            CATEGORY_ORDER.get(
                value,
                99,
            ),
            value.casefold(),
        ),
    ):
        print(
            f"  {category}: "
            f"{category_counts[category]}"
        )

    print(
        "=" * 72
    )

    sys.exit(0)


if __name__ == "__main__":
    main()