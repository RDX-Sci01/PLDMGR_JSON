#!/usr/bin/env python3
"""
generate.py — builds payloads.json from links.txt.

Features:
  - Resolves GitHub repositories to their latest published release
  - Supports # MANUAL GitHub entries
  - Supports pinned direct URLs
  - Supports multiple payload assets per GitHub release
  - Cleans Markdown/HTML descriptions
  - Validates payload URLs
  - Produces deterministic JSON
  - Only rewrites payloads.json when content changes
  - Never publishes an empty catalog

Exit codes:
  0 — success
  1 — links.txt missing/empty or fatal input error
  2 — no usable payloads could be generated
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"

GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    "",
)

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"

ASSET_EXTENSIONS = (
    ".elf",
    ".bin",
    ".lua",
)

HTTP_TIMEOUT = 20

DESCRIPTION_MAX_LENGTH = 180

DEFAULT_CATEGORY = "Utilities & Tools"


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API
# ─────────────────────────────────────────────────────────────────────────────

def github_headers() -> Dict[str, str]:
    """Return standard GitHub API headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PLDMGR-JSON-Generator/2.0",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = (
            f"Bearer {GITHUB_TOKEN}"
        )

    return headers


def fetch_json(url: str) -> object:
    """Fetch and decode JSON."""
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
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        try:
            parsed = json.loads(body)
            message = parsed.get(
                "message",
                exc.reason,
            )
        except Exception:
            message = exc.reason

        raise RuntimeError(
            f"HTTP {exc.code}: {message}"
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


# ─────────────────────────────────────────────────────────────────────────────
# HTTP validation
# ─────────────────────────────────────────────────────────────────────────────

def http_ok(url: str) -> bool:
    """
    Validate a payload URL.

    Uses a small GET request rather than HEAD because GitHub release
    assets/CDNs do not consistently support HEAD.
    """

    if not url:
        return False

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PLDMGR-JSON-Generator/2.0",
            "Range": "bytes=0-0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return 200 <= response.status < 400

    except urllib.error.HTTPError as exc:
        # Some servers ignore Range and return a normal successful
        # response. urllib may surface it as an HTTPError in unusual
        # redirect/server configurations, so accept successful codes.
        return 200 <= exc.code < 400

    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────

def clean_description(
    text: str,
    fallback: str = "",
) -> str:
    """Convert Markdown/HTML into short plain text."""

    if not text:
        text = fallback

    if not text:
        return ""

    text = html.unescape(
        str(text)
    )

    # HTML.
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

    # Markdown formatting.
    text = re.sub(
        r"[*_~]+",
        "",
        text,
    )

    # Quotes/headings.
    text = re.sub(
        r"^\s*[>#]+\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

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
        text = (
            text[:DESCRIPTION_MAX_LENGTH]
            .rstrip()
        )

        if " " in text:
            text = text.rsplit(
                " ",
                1,
            )[0]

        text += "..."

    return text


def first_release_description(
    release: dict,
) -> str:
    """Return the first useful release-note line."""

    body = release.get("body") or ""

    for line in body.splitlines():
        cleaned = clean_description(line)

        if cleaned:
            return cleaned

    return ""


def date_from_iso(
    value: Optional[str],
) -> str:
    """Convert an ISO timestamp to YYYY-MM-DD."""

    if value:
        match = re.match(
            r"^(\d{4}-\d{2}-\d{2})",
            value,
        )

        if match:
            return match.group(1)

    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# Payload metadata
# ─────────────────────────────────────────────────────────────────────────────

def asset_is_payload(name: str) -> bool:
    """Return True for supported payload extensions."""
    return bool(name) and name.lower().endswith(
        ASSET_EXTENSIONS
    )


def filename_without_extension(
    filename: str,
) -> str:
    """Return filename without its extension."""
    return os.path.splitext(
        os.path.basename(filename)
    )[0]


def normalize_payload_name(
    filename: str,
) -> str:
    """Create a clean catalog name from a filename."""

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

    # Remove PS4/PS5 suffix.
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


# ─────────────────────────────────────────────────────────────────────────────
# Categories
# ─────────────────────────────────────────────────────────────────────────────

def guess_category(
    name: str,
    description: str = "",
) -> str:
    """Assign a user-friendly category."""

    text = (
        f"{name} {description}"
    ).lower()

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


# ─────────────────────────────────────────────────────────────────────────────
# links.txt parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_links(
    path: str,
) -> List[Tuple[str, bool]]:
    """
    Parse links.txt.

    Returns:
        [(value, is_manual), ...]

    Supported:
        github:user/repo
        github:user/repo # MANUAL
        https://...
        http://...
    """

    entries = []
    seen = set()

    try:
        with open(
            path,
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
            f"ERROR: Could not read "
            f"{path}: {exc}"
        )
        sys.exit(1)

    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        is_manual = bool(
            re.search(
                r"\s*#\s*MANUAL\b",
                line,
                flags=re.IGNORECASE,
            )
        )

        value = re.sub(
            r"\s+#.*$",
            "",
            line,
        ).strip()

        if not value:
            continue

        key = value.casefold()

        if key in seen:
            print(
                f"  [SKIP] Duplicate entry: "
                f"{value}"
            )
            continue

        seen.add(key)

        if value.startswith(
            "github:"
        ):
            entries.append(
                (
                    value,
                    is_manual,
                )
            )

        elif value.startswith(
            ("http://", "https://")
        ):
            entries.append(
                (
                    value,
                    True,
                )
            )

        else:
            print(
                f"  [WARN] Unknown format, "
                f"skipping: {value}"
            )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# GitHub repository resolver
# ─────────────────────────────────────────────────────────────────────────────

def github_repo_from_entry(
    value: str,
) -> Optional[str]:
    """Extract owner/repo from github: entry."""

    repo = value[
        len("github:"):
    ].strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/"
        r"[A-Za-z0-9_.-]+",
        repo,
    ):
        return None

    return repo


def resolve_github(
    repo: str,
    manual: bool = False,
) -> List[dict]:
    """Resolve a repository to payload assets."""

    url = (
        f"{GITHUB_API}/repos/"
        f"{repo}/releases/latest"
    )

    try:
        release = fetch_json(url)

    except RuntimeError as exc:
        label = "MANUAL " if manual else ""

        print(
            f"  [WARN] {label}"
            f"github:{repo} — {exc}"
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
            and asset_is_payload(
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
            "latest release has no "
            "supported payload assets"
        )
        return []

    version = str(
        release.get(
            "tag_name"
        )
        or "unknown"
    ).strip()

    description = (
        first_release_description(
            release
        )
    )

    source = (
        f"https://github.com/"
        f"{repo}/releases"
    )

    entries = []

    for asset in payload_assets:
        filename = str(
            asset["name"]
        ).strip()

        direct_url = str(
            asset[
                "browser_download_url"
            ]
        ).strip()

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
                    release.get(
                        "published_at"
                    )
                    or release.get(
                        "created_at"
                    )
                ),
                "version": version,
                "category": guess_category(
                    name,
                    clean_desc,
                ),
                "_repo": repo.casefold(),
                "_origin": "github",
                "_manual": bool(manual),
            }
        )

    print(
        f"  [OK] github:{repo} — "
        f"{len(entries)} payload asset(s), "
        f"{version}"
    )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Direct URL resolver
# ─────────────────────────────────────────────────────────────────────────────

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
            f"  [WARN] Direct URL has no "
            f"filename: {url}"
        )
        return []

    if not asset_is_payload(
        filename
    ):
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
            "category": guess_category(
                name
            ),
            "_repo": None,
            "_origin": "direct",
            "_manual": True,
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def payload_identity(
    entry: dict,
) -> Tuple:
    """Build a stable identity."""

    repo = entry.get(
        "_repo"
    )

    filename = str(
        entry.get(
            "filename"
        )
        or ""
    ).casefold()

    version = str(
        entry.get(
            "version"
        )
        or ""
    ).casefold()

    if repo:
        return (
            "github",
            repo,
            version,
            filename,
        )

    return (
        "url",
        str(
            entry.get(
                "url"
            )
            or ""
        ).casefold(),
    )


def deduplicate(
    entries: List[dict],
) -> List[dict]:
    """Remove duplicate payloads deterministically."""

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


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_entries(
    entries: List[dict],
) -> List[dict]:
    """Validate payload URLs."""

    valid = []

    print()
    print(
        f"Validating {len(entries)} "
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


# ─────────────────────────────────────────────────────────────────────────────
# Sorting
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_ORDER = {
    "System & Jailbreak": 0,
    "Loaders": 1,
    "Networking & Servers": 2,
    "Utilities & Tools": 3,
}


def sort_entries(
    entries: List[dict],
) -> List[dict]:
    """Sort entries deterministically."""

    return sorted(
        entries,
        key=lambda entry: (
            CATEGORY_ORDER.get(
                entry.get(
                    "category",
                    "",
                ),
                99,
            ),
            str(
                entry.get(
                    "name",
                    "",
                )
            ).casefold(),
            str(
                entry.get(
                    "version",
                    "",
                )
            ).casefold(),
            str(
                entry.get(
                    "filename",
                    "",
                )
            ).casefold(),
            str(
                entry.get(
                    "url",
                    "",
                )
            ).casefold(),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON output
# ─────────────────────────────────────────────────────────────────────────────

def clean_internal_fields(
    entry: dict,
) -> dict:
    """Remove private generator metadata."""

    return {
        key: value
        for key, value in entry.items()
        if not key.startswith("_")
    }


def build_json(
    entries: List[dict],
) -> str:
    """Build deterministic JSON."""

    public_entries = [
        clean_internal_fields(entry)
        for entry in sort_entries(entries)
    ]

    return (
        json.dumps(
            public_entries,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_output(
    entries: List[dict],
) -> bool:
    """
    Write payloads.json only when changed.

    Refuses to create an empty catalog.
    """

    if not entries:
        raise RuntimeError(
            "refusing to write an empty "
            "payloads.json"
        )

    new_json = build_json(
        entries
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

    temporary = (
        OUTPUT_FILE + ".tmp"
    )

    try:
        with open(
            temporary,
            "w",
            encoding="utf-8",
        ) as file:
            file.write(new_json)
            file.flush()
            os.fsync(
                file.fileno()
            )

        os.replace(
            temporary,
            OUTPUT_FILE,
        )

    finally:
        if os.path.exists(
            temporary
        ):
            try:
                os.unlink(
                    temporary
                )
            except OSError:
                pass

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("PLDMGR payload generator")
    print("=" * 72)

    parsed_links = parse_links(
        LINKS_FILE
    )

    if not parsed_links:
        print(
            f"ERROR: {LINKS_FILE} contains "
            "no usable entries"
        )
        sys.exit(1)

    github_entries = []
    direct_entries = []

    github_seen = set()
    direct_seen = set()

    print()
    print("Resolving sources...")

    for value, manual in parsed_links:
        print(
            f"\nProcessing: {value}"
        )

        if value.startswith(
            "github:"
        ):
            repo = github_repo_from_entry(
                value
            )

            if not repo:
                print(
                    f"  [WARN] Invalid "
                    f"GitHub repository: "
                    f"{value}"
                )
                continue

            key = repo.casefold()

            if key in github_seen:
                print(
                    f"  [SKIP] Duplicate "
                    f"GitHub repository: "
                    f"{repo}"
                )
                continue

            github_seen.add(key)

            if manual:
                print(
                    "  [INFO] Protected "
                    "MANUAL repository"
                )

            github_entries.extend(
                resolve_github(
                    repo,
                    manual=manual,
                )
            )

        elif value.startswith(
            ("http://", "https://")
        ):
            key = value.casefold()

            if key in direct_seen:
                print(
                    f"  [SKIP] Duplicate "
                    f"direct URL"
                )
                continue

            direct_seen.add(key)

            direct_entries.extend(
                resolve_direct(
                    value
                )
            )

    print()
    print("=" * 72)
    print("Resolution summary")
    print("=" * 72)

    print(
        f"GitHub payload entries : "
        f"{len(github_entries)}"
    )

    print(
        f"Direct URL entries     : "
        f"{len(direct_entries)}"
    )

    merged = deduplicate(
        github_entries
        + direct_entries
    )

    print(
        f"Unique entries         : "
        f"{len(merged)}"
    )

    if not merged:
        print(
            "\nERROR: No payloads could "
            "be resolved."
        )
        print(
            "payloads.json was not modified."
        )
        sys.exit(2)

    valid = validate_entries(
        merged
    )

    print()
    print(
        f"Valid payloads: "
        f"{len(valid)} / {len(merged)}"
    )

    if not valid:
        print(
            "\nERROR: No payload URLs "
            "passed validation."
        )
        print(
            "payloads.json was not modified."
        )
        sys.exit(2)

    # Final duplicate protection.
    valid = deduplicate(
        valid
    )

    # Never allow an empty output.
    if not valid:
        print(
            "\nERROR: Final payload list "
            "is empty."
        )
        print(
            "payloads.json was not modified."
        )
        sys.exit(2)

    try:
        changed = write_output(
            valid
        )

    except Exception as exc:
        print(
            f"\nERROR: Could not write "
            f"{OUTPUT_FILE}: {exc}"
        )
        sys.exit(2)

    print()
    print("=" * 72)

    if changed:
        print(
            f"Wrote {len(valid)} payloads "
            f"to {OUTPUT_FILE}"
        )
    else:
        print(
            f"No changes detected — "
            f"{OUTPUT_FILE} not updated"
        )

    # Category summary.
    category_counts: Dict[
        str,
        int,
    ] = {}

    for entry in valid:
        category = entry.get(
            "category",
            DEFAULT_CATEGORY,
        )

        category_counts[
            category
        ] = (
            category_counts.get(
                category,
                0,
            )
            + 1
        )

    print()
    print("Catalog summary:")

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

    print("=" * 72)

    sys.exit(0)


if __name__ == "__main__":
    main()