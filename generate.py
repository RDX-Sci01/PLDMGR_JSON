#!/usr/bin/env python3
"""
generate.py — builds payloads.json from links.txt

Production features:
  - Resolves GitHub repositories to their latest published release
  - Supports # MANUAL GitHub entries
  - Supports pinned direct URLs
  - Supports mirror:<user>/<repo>@<tag>
  - Supports the itsPLK consolidated mirror payloads.json
  - Uses mirrors as per-payload fallbacks, not duplicate sources
  - Removes duplicate upstream/mirror payloads
  - Falls back to mirrors when an upstream asset fails validation
  - Handles GitHub releases containing multiple payload assets
  - Cleans Markdown/HTML from descriptions
  - Validates URLs with HEAD, then GET/Range fallback
  - Validates mirror entries before they can be used
  - Keeps source/source_direct metadata consistent
  - Produces deterministic JSON
  - Does not use the current date for pinned entries
  - Writes payloads.json atomically
  - Only rewrites payloads.json when content actually changes

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

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

LINKS_FILE = "links.txt"
OUTPUT_FILE = "payloads.json"

ASSET_EXTENSIONS = (".elf", ".bin", ".lua")

HTTP_TIMEOUT = 15

DESCRIPTION_MAX_LENGTH = 180

DEFAULT_CATEGORY = "Utilities & Tools"

USER_AGENT = "PLDMGR-JSON-Generator/1.0"

UNKNOWN_DATE = "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP / GitHub helpers
# ─────────────────────────────────────────────────────────────────────────────

def gh_headers() -> Dict[str, str]:
    """Return standard GitHub API headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def fetch_json(url: str, github: bool = False) -> object:
    """Fetch and decode JSON from a URL."""
    headers = gh_headers() if github else {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=HTTP_TIMEOUT,
    ) as response:
        data = response.read()

    return json.loads(data.decode("utf-8"))


def http_ok(url: str) -> bool:
    """
    Validate a download URL.

    Strategy:
      1. HEAD
      2. GET with Range: bytes=0-0
      3. Normal GET fallback for servers that ignore Range

    urllib follows redirects automatically.
    """
    if not url:
        return False

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.netloc:
        return False

    headers = {
        "User-Agent": USER_AGENT,
    }

    # ── HEAD ────────────────────────────────────────────────────────────

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

    # ── GET with Range ─────────────────────────────────────────────────

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
        pass

    # ── Normal GET fallback ─────────────────────────────────────────────
    #
    # Some hosts ignore Range and some unusual CDNs reject both HEAD
    # and Range requests.

    try:
        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return 200 <= response.status < 400

    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Text / metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def clean_description(
    text: str,
    fallback: str = "",
) -> str:
    """
    Convert GitHub Markdown/HTML into short plain text suitable for the UI.
    """
    if not text:
        text = fallback

    if not text:
        return ""

    text = html.unescape(str(text))

    # Remove HTML comments.
    text = re.sub(
        r"<!--.*?-->",
        " ",
        text,
        flags=re.DOTALL,
    )

    # Remove HTML tags.
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

    # Markdown links — retain visible text.
    text = re.sub(
        r"\[([^\]]+)\]\([^)]*\)",
        r"\1",
        text,
    )

    # Reference-style links.
    text = re.sub(
        r"\[([^\]]+)\]\[[^\]]*\]",
        r"\1",
        text,
    )

    # URLs.
    text = re.sub(
        r"https?://\S+",
        " ",
        text,
    )

    # Inline code.
    text = re.sub(
        r"`([^`]*)`",
        r"\1",
        text,
    )

    # Markdown emphasis / strike-through.
    text = re.sub(
        r"[*_~]+",
        "",
        text,
    )

    # Markdown headings / blockquotes / list markers.
    text = re.sub(
        r"^\s*[>#]+\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^\s*[-+*]\s+",
        "",
        text,
        flags=re.MULTILINE,
    )

    text = re.sub(
        r"^\s*\d+\.\s+",
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

    if not text:
        return ""

    if len(text) > DESCRIPTION_MAX_LENGTH:
        text = text[:DESCRIPTION_MAX_LENGTH].rstrip()

        if " " in text:
            text = text.rsplit(" ", 1)[0]

        text += "..."

    return text


def first_release_description(release: dict) -> str:
    """Extract the first useful line from release notes."""
    body = release.get("body") or ""

    for line in body.splitlines():
        cleaned = clean_description(line)

        if cleaned:
            return cleaned

    return ""


def parse_version(tag: str) -> str:
    """Return a normalized release tag."""
    tag = str(tag or "").strip()

    return tag or "unknown"


def date_from_iso(value: Optional[str]) -> str:
    """
    Convert an ISO timestamp to YYYY-MM-DD.

    Unlike the previous implementation, missing dates do not use the
    current date because doing so would make output non-deterministic.
    """
    if not value:
        return UNKNOWN_DATE

    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})",
        str(value),
    )

    if match:
        return match.group(1)

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        return parsed.astimezone(
            timezone.utc
        ).strftime("%Y-%m-%d")

    except Exception:
        return UNKNOWN_DATE


def asset_is_payload(name: str) -> bool:
    """Return True for supported payload asset extensions."""
    if not name:
        return False

    return str(name).lower().endswith(
        ASSET_EXTENSIONS
    )


def repo_key(repo: str) -> str:
    """Normalize a GitHub repository identifier."""
    return (
        str(repo)
        .strip()
        .strip("/")
        .lower()
    )


def github_repo_from_source(
    source: str,
) -> Optional[str]:
    """
    Extract owner/repo from a GitHub URL.
    """
    if not source:
        return None

    match = re.search(
        r"github\.com/([^/]+/[^/#?]+)",
        str(source),
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    repo = match.group(1)

    # Remove URL path suffixes if present.
    repo = repo.split("/releases", 1)[0]
    repo = repo.split("/tags", 1)[0]
    repo = repo.split("/download", 1)[0]

    return repo_key(repo)


def github_release_identity(
    source: str,
    version: str,
) -> Optional[Tuple[str, str]]:
    """Return normalized repository + release identity."""
    repo = github_repo_from_source(source)

    if not repo:
        return None

    return (
        repo,
        str(version or "").strip().lower(),
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
    Produce a reasonable catalog name from an asset filename.
    """
    name = filename_without_extension(filename)

    # Remove common version suffixes.
    name = re.sub(
        r"([_\-\s]+)v?\d+(?:\.\d+)*(?:[-_a-zA-Z0-9]*)?$",
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

    name = name.strip("_- ")

    return name or filename_without_extension(
        filename
    )


def guess_category(
    name: str,
    description: str = "",
) -> str:
    """
    Guess a user-friendly catalog category.

    Loader rules run before networking rules so names such as
    webkit-autoloader remain in Loaders.
    """
    text = (
        f"{name} {description}"
    ).lower()

    # ── System / Jailbreak ─────────────────────────────────────────────

    system_keywords = (
        "jailbreak",
        "kstuff",
        "etahen",
        "eta hen",
        "exploit",
        "lapy",
        "daemon",
    )

    if any(
        keyword in text
        for keyword in system_keywords
    ):
        return "System & Jailbreak"

    # ── Loaders ────────────────────────────────────────────────────────

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

    # ── Networking ─────────────────────────────────────────────────────

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

    Supported:
      github:owner/repo
      github:owner/repo # MANUAL
      mirror:owner/repo@tag
      https://...
    """
    entries: List[Tuple[str, bool]] = []
    seen = set()

    try:
        with open(
            path,
            encoding="utf-8",
        ) as file:
            lines = file.readlines()

    except FileNotFoundError:
        print(f"ERROR: {path} not found")
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

        # Remove inline comments only when preceded by whitespace.
        value = re.sub(
            r"\s+#.*$",
            "",
            line,
        ).strip()

        if not value:
            continue

        key = value.lower()

        if key in seen:
            print(
                f"  [SKIP] Duplicate links.txt entry: "
                f"{value}"
            )
            continue

        seen.add(key)
        entries.append(
            (value, is_manual)
        )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# GitHub resolver
# ─────────────────────────────────────────────────────────────────────────────

def resolve_github(
    repo: str,
    manual: bool = False,
) -> List[dict]:
    """
    Resolve a GitHub repository to all supported payload assets from
    its latest published release.
    """
    repo = repo.strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
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
        release = fetch_json(
            url,
            github=True,
        )

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            label = "MANUAL " if manual else ""

            print(
                f"  [WARN] {label}github:{repo} — "
                "no published latest release"
            )
        else:
            print(
                f"  [WARN] github:{repo} — "
                f"HTTP {exc.code}"
            )

        return []

    except Exception as exc:
        print(
            f"  [WARN] github:{repo} — "
            f"API error: {exc}"
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

    assets = release.get("assets") or []

    payload_assets = []

    for asset in assets:
        if not isinstance(asset, dict):
            continue

        filename = str(
            asset.get("name") or ""
        ).strip()

        direct_url = str(
            asset.get("browser_download_url")
            or ""
        ).strip()

        if (
            asset_is_payload(filename)
            and direct_url
        ):
            payload_assets.append(
                (filename, direct_url)
            )

    if not payload_assets:
        print(
            f"  [WARN] github:{repo} — "
            "latest release contains no "
            "supported payload assets"
        )
        return []

    tag = parse_version(
        release.get("tag_name", "")
    )

    release_description = (
        first_release_description(release)
    )

    source = (
        f"https://github.com/"
        f"{repo}/releases"
    )

    last_update = date_from_iso(
        release.get("published_at")
        or release.get("created_at")
    )

    entries: List[dict] = []

    for filename, direct_url in payload_assets:
        name = normalize_payload_name(
            filename
        )

        description = clean_description(
            release_description,
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
                    description
                    or f"{name} payload"
                ),
                "last_update": last_update,
                "version": tag,
                "category": guess_category(
                    name,
                    description,
                ),
                "_repo": repo_key(repo),
                "_origin": "github",
                "_manual": bool(manual),
            }
        )

    print(
        f"  [OK] github:{repo} — "
        f"{len(entries)} payload asset(s), "
        f"{tag}"
    )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Mirror resolver
# ─────────────────────────────────────────────────────────────────────────────

ITSPLK_MIRROR_JSON = (
    "https://itsplk.github.io/"
    "ps5-payloads-mirror/payloads.json"
)


def load_itsplk_mirror_json() -> List[dict]:
    """Load the hosted itsPLK mirror catalog."""
    try:
        data = fetch_json(
            ITSPLK_MIRROR_JSON
        )

    except Exception as exc:
        print(
            f"  [WARN] mirror — "
            f"could not fetch hosted payloads.json: "
            f"{exc}"
        )
        return []

    if not isinstance(
        data,
        list,
    ):
        print(
            "  [WARN] mirror — hosted "
            "payloads.json is not a list"
        )
        return []

    print(
        f"  [OK] mirror — loaded "
        f"{len(data)} hosted entries"
    )

    return data


def normalize_mirror_entry(
    raw: dict,
) -> Optional[dict]:
    """
    Normalize and sanity-check a mirror catalog entry.

    Important:
      The mirror URL is the actual download URL.
      source_direct is metadata pointing to the original upstream
      release whenever available.
    """
    if not isinstance(
        raw,
        dict,
    ):
        return None

    url = str(
        raw.get("url") or ""
    ).strip()

    if not url:
        return None

    parsed = urlparse(url)

    if parsed.scheme not in (
        "http",
        "https",
    ):
        return None

    if not parsed.netloc:
        return None

    filename = str(
        raw.get("filename")
        or unquote(
            os.path.basename(
                parsed.path
            )
        )
        or ""
    ).strip()

    if not asset_is_payload(
        filename
    ):
        return None

    source = str(
        raw.get("source") or ""
    ).strip()

    source_direct = str(
        raw.get("source_direct") or ""
    ).strip()

    # If source_direct is absent, use source when available.
    if not source_direct:
        source_direct = source

    # The actual mirror URL must always remain the catalog URL.
    # Do not replace it with source_direct.
    name = str(
        raw.get("name")
        or normalize_payload_name(
            filename
        )
    ).strip()

    version = parse_version(
        raw.get("version")
    )

    description = clean_description(
        str(
            raw.get("description") or ""
        ),
        fallback=f"{name} payload",
    )

    repo = (
        github_repo_from_source(
            source_direct
        )
        or github_repo_from_source(
            source
        )
    )

    category = str(
        raw.get("category")
        or guess_category(
            name,
            description,
        )
    ).strip()

    # Never allow an empty category.
    if not category:
        category = guess_category(
            name,
            description,
        )

    last_update = date_from_iso(
        raw.get("last_update")
    )

    return {
        "name": name,
        "filename": filename,
        "url": url,
        "source": source or url,
        "source_direct": (
            source_direct or url
        ),
        "description": (
            description
            or f"{name} payload"
        ),
        "last_update": last_update,
        "version": version,
        "category": category,
        "_repo": repo,
        "_origin": "mirror",
        "_manual": False,
    }


def resolve_mirror(
    spec: str,
) -> List[dict]:
    """
    Resolve a mirror source.

    Special case:
      mirror:itsPLK/ps5-payloads-mirror@...
      loads the consolidated hosted payloads.json.

    Generic:
      mirror:user/repo@tag
      resolves that exact GitHub release.
    """
    spec = spec.strip()

    if spec.lower().startswith(
        "itsplk/ps5-payloads-mirror@"
    ):
        raw_entries = (
            load_itsplk_mirror_json()
        )

        entries: List[dict] = []

        for raw in raw_entries:
            entry = normalize_mirror_entry(
                raw
            )

            if entry:
                entries.append(entry)

        print(
            f"  [OK] mirror:{spec} — "
            f"{len(entries)} usable entries"
        )

        return entries

    match = re.fullmatch(
        r"([^@]+)@(.+)",
        spec,
    )

    if not match:
        print(
            f"  [WARN] mirror:{spec} — "
            "expected user/repo@tag"
        )
        return []

    repo = match.group(1).strip()
    tag = match.group(2).strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        repo,
    ):
        print(
            f"  [WARN] mirror:{spec} — "
            "invalid repository"
        )
        return []

    api_url = (
        f"{GITHUB_API}/repos/"
        f"{repo}/releases/tags/"
        f"{tag}"
    )

    try:
        release = fetch_json(
            api_url,
            github=True,
        )

    except Exception as exc:
        print(
            f"  [WARN] mirror:{spec} — "
            f"API error: {exc}"
        )
        return []

    if not isinstance(
        release,
        dict,
    ):
        return []

    entries: List[dict] = []

    release_tag = parse_version(
        release.get(
            "tag_name",
            tag,
        )
    )

    source = (
        f"https://github.com/"
        f"{repo}/releases"
    )

    last_update = date_from_iso(
        release.get("published_at")
        or release.get("created_at")
    )

    release_description = (
        first_release_description(
            release
        )
    )

    for asset in (
        release.get("assets") or []
    ):
        if not isinstance(
            asset,
            dict,
        ):
            continue

        filename = str(
            asset.get("name") or ""
        ).strip()

        direct_url = str(
            asset.get(
                "browser_download_url"
            )
            or ""
        ).strip()

        if (
            not asset_is_payload(
                filename
            )
            or not direct_url
        ):
            continue

        name = normalize_payload_name(
            filename
        )

        description = clean_description(
            release_description,
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
                    description
                    or f"{name} payload"
                ),
                "last_update": last_update,
                "version": release_tag,
                "category": guess_category(
                    name,
                    description,
                ),
                "_repo": repo_key(repo),
                "_origin": "mirror",
                "_manual": False,
            }
        )

    print(
        f"  [OK] mirror:{spec} — "
        f"{len(entries)} payload asset(s)"
    )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Direct URL resolver
# ─────────────────────────────────────────────────────────────────────────────

def resolve_direct(
    url: str,
    manual: bool = True,
) -> List[dict]:
    """Resolve a pinned direct payload URL."""
    parsed = urlparse(url)

    if parsed.scheme not in (
        "http",
        "https",
    ) or not parsed.netloc:
        print(
            f"  [WARN] invalid direct URL: "
            f"{url}"
        )
        return []

    filename = unquote(
        os.path.basename(
            parsed.path
        )
    ).strip()

    if not filename:
        print(
            f"  [WARN] direct URL has no "
            f"filename: {url}"
        )
        return []

    if not asset_is_payload(
        filename
    ):
        print(
            f"  [WARN] unsupported direct "
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
            "last_update": UNKNOWN_DATE,
            "version": "pinned",
            "category": guess_category(
                name
            ),
            "_repo": None,
            "_origin": "direct",
            "_manual": bool(manual),
        }
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Candidate identity / deduplication
# ─────────────────────────────────────────────────────────────────────────────

def release_family(
    entry: dict,
) -> Optional[Tuple[str, str]]:
    """
    Return repository + version identity.

    For mirror entries, _repo is derived from source_direct/source.
    """
    repo = entry.get("_repo")

    if not repo:
        repo = (
            github_repo_from_source(
                str(
                    entry.get(
                        "source_direct"
                    )
                    or ""
                )
            )
            or github_repo_from_source(
                str(
                    entry.get(
                        "source"
                    )
                    or ""
                )
            )
        )

    if not repo:
        return None

    version = str(
        entry.get("version")
        or ""
    ).strip().lower()

    if not version:
        return None

    return (
        repo_key(repo),
        version,
    )


def payload_asset_key(
    entry: dict,
) -> Tuple:
    """
    Stable identity for one payload asset.

    Repository + release + filename is preferred.
    Direct/pinned URLs use their URL.
    """
    family = release_family(entry)

    filename = str(
        entry.get("filename")
        or ""
    ).strip().lower()

    if family:
        return (
            "asset",
            family[0],
            family[1],
            filename,
        )

    return (
        "url",
        str(
            entry.get("url")
            or ""
        ).strip().lower(),
    )


def same_payload_family(
    a: dict,
    b: dict,
) -> bool:
    """Return True if two entries are the same repo/release."""
    family_a = release_family(a)
    family_b = release_family(b)

    return (
        family_a is not None
        and family_a == family_b
    )


def clean_internal_fields(
    entry: dict,
) -> dict:
    """Remove generator-only metadata."""
    return {
        key: value
        for key, value in entry.items()
        if not key.startswith("_")
    }


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_entries(
    entries: List[dict],
    label: str,
) -> List[dict]:
    """
    Validate payload URLs.

    Validation occurs BEFORE upstream/mirror merging so that a broken
    upstream asset cannot suppress a valid mirror fallback.
    """
    valid: List[dict] = []

    if not entries:
        return valid

    print(
        f"\nValidating {len(entries)} "
        f"{label} payload URL(s)..."
    )

    seen_urls = set()

    for entry in entries:
        url = str(
            entry.get("url") or ""
        ).strip()

        name = entry.get(
            "name",
            "unknown",
        )

        filename = entry.get(
            "filename",
            "",
        )

        if not url:
            print(
                f"  [SKIP] {name} — "
                "missing URL"
            )
            continue

        url_key = url.lower()

        if url_key in seen_urls:
            continue

        seen_urls.add(url_key)

        if http_ok(url):
            valid.append(entry)

            print(
                f"  [OK] {name} — "
                f"{filename}"
            )
        else:
            print(
                f"  [SKIP] {name} — "
                f"URL unreachable: {url}"
            )

    return valid


# ─────────────────────────────────────────────────────────────────────────────
# Upstream / mirror merge
# ─────────────────────────────────────────────────────────────────────────────

def merge_entries(
    github_entries: List[dict],
    mirror_entries: List[dict],
    direct_entries: List[dict],
) -> List[dict]:
    """
    Merge already-validated entries.

    Priority:
      1. Direct pinned URLs
      2. Valid GitHub upstream assets
      3. Valid mirror assets

    Mirror fallback is decided PER ASSET, not per repository.

    Therefore:
      - If 3 upstream assets exist and 1 is broken, the mirror can
        supply that one missing asset.
      - A valid upstream asset suppresses only its corresponding
        mirror asset.
      - Different assets from the same release are retained.
    """
    result: List[dict] = []

    # Direct URLs are independent.
    result.extend(direct_entries)

    # Keep valid upstream assets.
    result.extend(github_entries)

    # Track every valid upstream asset.
    upstream_keys = set()

    for entry in github_entries:
        upstream_keys.add(
            payload_asset_key(entry)
        )

    # Add only mirror assets that do not have a valid upstream
    # equivalent.
    for mirror in mirror_entries:
        key = payload_asset_key(
            mirror
        )

        if key in upstream_keys:
            continue

        result.append(mirror)

    # Final exact identity deduplication.
    unique: Dict[Tuple, dict] = {}

    for entry in result:
        key = payload_asset_key(entry)

        existing = unique.get(key)

        if existing is None:
            unique[key] = entry
            continue

        # Explicitly prefer direct > github > mirror.
        priority = {
            "direct": 3,
            "github": 2,
            "mirror": 1,
        }

        current_priority = priority.get(
            entry.get("_origin"),
            0,
        )

        existing_priority = priority.get(
            existing.get("_origin"),
            0,
        )

        if current_priority > existing_priority:
            unique[key] = entry

    return list(
        unique.values()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic ordering
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
    """Sort payloads deterministically."""
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
            ).lower(),
            str(
                entry.get(
                    "version",
                    "",
                )
            ).lower(),
            str(
                entry.get(
                    "filename",
                    "",
                )
            ).lower(),
            str(
                entry.get(
                    "url",
                    "",
                )
            ).lower(),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON writing
# ─────────────────────────────────────────────────────────────────────────────

def write_output(
    entries: List[dict],
) -> bool:
    """
    Write payloads.json only when content changed.

    Uses an atomic temporary file replacement.
    """
    public_entries = [
        clean_internal_fields(entry)
        for entry in sort_entries(entries)
    ]

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

        os.replace(
            temporary_file,
            OUTPUT_FILE,
        )

    except Exception:
        try:
            if os.path.exists(
                temporary_file
            ):
                os.remove(
                    temporary_file
                )
        except OSError:
            pass

        raise

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(
        "PLDMGR payload generator"
    )
    print("=" * 72)

    parsed_links = parse_links(
        LINKS_FILE
    )

    if not parsed_links:
        print(
            f"ERROR: {LINKS_FILE} is empty"
        )
        sys.exit(1)

    github_entries: List[dict] = []
    mirror_entries: List[dict] = []
    direct_entries: List[dict] = []

    github_repos_seen = set()
    mirror_specs_seen = set()
    direct_urls_seen = set()

    # ── Resolve sources ────────────────────────────────────────────────

    for value, is_manual in parsed_links:
        print(
            f"\nProcessing: {value}"
        )

        if value.startswith(
            "github:"
        ):
            repo = value[7:].strip()
            key = repo_key(repo)

            if key in github_repos_seen:
                print(
                    f"  [SKIP] Duplicate "
                    f"GitHub repository: {repo}"
                )
                continue

            github_repos_seen.add(key)

            if is_manual:
                print(
                    "  [INFO] Protected "
                    "manual repository"
                )

            github_entries.extend(
                resolve_github(
                    repo,
                    manual=is_manual,
                )
            )

        elif value.startswith(
            "mirror:"
        ):
            spec = value[7:].strip()
            key = spec.lower()

            if key in mirror_specs_seen:
                print(
                    f"  [SKIP] Duplicate "
                    f"mirror: {spec}"
                )
                continue

            mirror_specs_seen.add(key)

            mirror_entries.extend(
                resolve_mirror(
                    spec
                )
            )

        elif (
            value.startswith(
                "http://"
            )
            or value.startswith(
                "https://"
            )
        ):
            key = value.lower()

            if key in direct_urls_seen:
                print(
                    f"  [SKIP] Duplicate "
                    f"direct URL: {value}"
                )
                continue

            direct_urls_seen.add(key)

            direct_entries.extend(
                resolve_direct(
                    value,
                    manual=is_manual,
                )
            )

        else:
            print(
                f"  [WARN] Unknown format, "
                f"skipping: {value}"
            )

    print(
        "\n" + "=" * 72
    )
    print(
        "Resolution summary"
    )
    print("=" * 72)

    print(
        f"GitHub upstream entries : "
        f"{len(github_entries)}"
    )

    print(
        f"Mirror entries           : "
        f"{len(mirror_entries)}"
    )

    print(
        f"Direct URL entries       : "
        f"{len(direct_entries)}"
    )

    # ── Validate BEFORE merging ────────────────────────────────────────
    #
    # This is critical.
    #
    # Previously, a broken upstream URL could suppress its mirror
    # equivalent because merging happened before validation.

    valid_github = validate_entries(
        github_entries,
        "GitHub",
    )

    valid_mirror = validate_entries(
        mirror_entries,
        "mirror",
    )

    valid_direct = validate_entries(
        direct_entries,
        "direct",
    )

    print(
        "\n" + "=" * 72
    )
    print(
        "Validation summary"
    )
    print("=" * 72)

    print(
        f"Valid GitHub entries : "
        f"{len(valid_github)}"
    )

    print(
        f"Valid mirror entries : "
        f"{len(valid_mirror)}"
    )

    print(
        f"Valid direct entries : "
        f"{len(valid_direct)}"
    )

    # ── Merge ──────────────────────────────────────────────────────────

    merged_entries = merge_entries(
        github_entries=valid_github,
        mirror_entries=valid_mirror,
        direct_entries=valid_direct,
    )

    print(
        f"Merged unique entries : "
        f"{len(merged_entries)}"
    )

    if not merged_entries:
        print(
            "\nERROR: No usable payloads "
            "could be generated"
        )
        sys.exit(2)

    # ── Final URL deduplication ───────────────────────────────────────

    final_entries: List[dict] = []
    seen_urls = set()

    for entry in sort_entries(
        merged_entries
    ):
        url = str(
            entry.get("url") or ""
        ).strip()

        if not url:
            continue

        url_key = url.lower()

        if url_key in seen_urls:
            continue

        seen_urls.add(url_key)

        final_entries.append(
            entry
        )

    if not final_entries:
        print(
            "\nERROR: No payloads remain "
            "after final filtering"
        )
        sys.exit(2)

    # ── Write ──────────────────────────────────────────────────────────

    try:
        changed = write_output(
            final_entries
        )

    except OSError as exc:
        print(
            f"\nERROR: Could not write "
            f"{OUTPUT_FILE}: {exc}"
        )
        sys.exit(2)

    print(
        "\n" + "=" * 72
    )

    if changed:
        print(
            f"Wrote {len(final_entries)} "
            f"payloads to {OUTPUT_FILE}"
        )
    else:
        print(
            f"No changes detected — "
            f"{OUTPUT_FILE} not updated"
        )

    # ── Category summary ───────────────────────────────────────────────

    category_counts: Dict[
        str,
        int,
    ] = {}

    for entry in final_entries:
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
            value.lower(),
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