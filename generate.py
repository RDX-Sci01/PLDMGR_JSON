#!/usr/bin/env python3
"""
generate.py — builds payloads.json from links.txt

Features:
  - Resolves GitHub repositories to their latest published release
  - Supports # MANUAL GitHub entries
  - Supports pinned direct URLs
  - Supports mirror:<user>/<repo>@<tag>
  - Uses the mirror as a fallback, not as a duplicate source
  - Removes duplicate upstream/mirror payloads
  - Cleans Markdown/HTML from descriptions
  - Validates URLs with HEAD, then GET/Range fallback
  - Handles GitHub releases containing multiple payload assets
  - Produces deterministic JSON output
  - Only rewrites payloads.json when content actually changes

Exit codes:
  0 — success
  1 — links.txt missing or empty / fatal input error
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

# Maximum description length exposed to the payload manager.
DESCRIPTION_MAX_LENGTH = 180

DEFAULT_CATEGORY = "Utilities & Tools"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP / GitHub helpers
# ─────────────────────────────────────────────────────────────────────────────

def gh_headers() -> Dict[str, str]:
    """
    Return standard GitHub API headers.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PLDMGR-JSON-Generator",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def fetch_json(url: str) -> object:
    """
    Fetch and decode JSON from a URL.
    """
    request = urllib.request.Request(
        url,
        headers=gh_headers(),
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        data = response.read()

    return json.loads(data.decode("utf-8"))


def http_ok(url: str) -> bool:
    """
    Validate a download URL.

    Strategy:
      1. HEAD request
      2. If HEAD fails, GET with Range: bytes=0-0

    GitHub release URLs redirect, which urllib follows automatically.
    """
    if not url:
        return False

    # First try HEAD.
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "PLDMGR-JSON-Generator",
            },
            method="HEAD",
        )

        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return 200 <= response.status < 400

    except Exception:
        pass

    # Some servers/CDNs do not support HEAD.
    # Fall back to a minimal GET request.
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "PLDMGR-JSON-Generator",
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


# ─────────────────────────────────────────────────────────────────────────────
# Text / metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def clean_description(text: str, fallback: str = "") -> str:
    """
    Convert GitHub Markdown/HTML into short plain text suitable for the UI.
    """
    if not text:
        text = fallback

    if not text:
        return ""

    text = html.unescape(str(text))

    # Remove HTML tags.
    text = re.sub(r"<[^>]+>", " ", text)

    # Markdown images.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)

    # Markdown links: keep visible text.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)

    # Inline code.
    text = re.sub(r"`([^`]*)`", r"\1", text)

    # Bold / italic markers.
    text = re.sub(r"[*_~]+", "", text)

    # Blockquote markers.
    text = re.sub(r"^\s*[>#]+\s*", "", text, flags=re.MULTILINE)

    # Markdown headings.
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)

    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > DESCRIPTION_MAX_LENGTH:
        text = text[:DESCRIPTION_MAX_LENGTH].rstrip()

        # Avoid cutting in the middle of a word.
        if " " in text:
            text = text.rsplit(" ", 1)[0]

        text += "..."

    return text


def first_release_description(release: dict) -> str:
    """
    Extract the first useful line from release notes.
    """
    body = release.get("body") or ""

    for line in body.splitlines():
        line = clean_description(line)
        if line:
            return line

    return ""


def parse_version(tag: str) -> str:
    """
    Keep the upstream tag as the displayed version.
    """
    return tag.strip() if tag else "unknown"


def date_from_iso(value: Optional[str]) -> str:
    """
    Convert an ISO timestamp to YYYY-MM-DD.
    """
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)

    if match:
        return match.group(1)

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def asset_is_payload(name: str) -> bool:
    """
    True for supported payload asset extensions.
    """
    if not name:
        return False

    return name.lower().endswith(ASSET_EXTENSIONS)


def repo_key(repo: str) -> str:
    """
    Normalize a GitHub repository identifier.
    """
    return repo.strip().strip("/").lower()


def github_repo_from_source(source: str) -> Optional[str]:
    """
    Extract owner/repo from a GitHub releases/source URL.
    """
    if not source:
        return None

    match = re.search(
        r"github\.com/([^/]+/[^/]+)",
        source,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return repo_key(match.group(1))


def filename_without_extension(filename: str) -> str:
    """
    Return filename without its final extension.
    """
    return os.path.splitext(os.path.basename(filename))[0]


def normalize_payload_name(filename: str) -> str:
    """
    Produce a reasonable catalog name from an asset filename.

    Examples:
      elfldr-ps5.elf       -> elfldr
      elfldr_v0.24.elf     -> elfldr
      BFpilot_v0.4.4.elf   -> BFpilot
    """
    name = filename_without_extension(filename)

    # Remove common version suffixes.
    name = re.sub(
        r"([_\-\s]+)v?\d+(?:\.\d+)*(?:[-_a-zA-Z0-9]*)?$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Remove common PS4/PS5 suffixes.
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

    return name or filename_without_extension(filename)


def guess_category(name: str, description: str = "") -> str:
    """
    Guess a user-friendly catalog category.

    Loader rules intentionally run before networking rules so that
    names such as 'webkit-autoloader' do not get classified as
    Networking & Servers merely because they contain 'web'.
    """
    text = f"{name} {description}".lower()

    # System / jailbreak.
    system_keywords = (
        "jailbreak",
        "kstuff",
        "etahen",
        "hen",
        "exploit",
        "lapy",
        "daemon",
    )

    if any(keyword in text for keyword in system_keywords):
        return "System & Jailbreak"

    # Loaders.
    loader_keywords = (
        "loader",
        "elfldr",
        "autoloader",
        "webkit",
        "elf loader",
    )

    if any(keyword in text for keyword in loader_keywords):
        return "Loaders"

    # Networking / servers.
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

    if any(keyword in text for keyword in networking_keywords):
        return "Networking & Servers"

    return DEFAULT_CATEGORY


# ─────────────────────────────────────────────────────────────────────────────
# links.txt parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_links(path: str) -> List[Tuple[str, bool]]:
    """
    Parse links.txt.

    Returns:
        [(value, is_manual), ...]

    Rules:
      - Blank lines ignored
      - Full-line comments ignored
      - Inline comments removed
      - # MANUAL is detected before removing comments
      - Direct URLs are preserved as pinned entries
    """
    entries: List[Tuple[str, bool]] = []
    seen = set()

    try:
        with open(path, encoding="utf-8") as file:
            lines = file.readlines()

    except FileNotFoundError:
        print(f"ERROR: {path} not found")
        sys.exit(1)

    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        is_manual = bool(
            re.search(r"\s*#\s*MANUAL\b", line, flags=re.IGNORECASE)
        )

        # Remove inline comments.
        value = re.sub(r"\s+#.*$", "", line).strip()

        if not value:
            continue

        key = value.lower()

        if key in seen:
            print(f"  [SKIP] Duplicate links.txt entry: {value}")
            continue

        seen.add(key)
        entries.append((value, is_manual))

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# GitHub resolver
# ─────────────────────────────────────────────────────────────────────────────

def resolve_github(repo: str, manual: bool = False) -> List[dict]:
    """
    Resolve a GitHub repository to all supported payload assets from
    its latest published release.

    GitHub's /releases/latest endpoint returns the latest published
    non-draft/non-prerelease release.
    """
    repo = repo.strip()

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        repo,
    ):
        print(f"  [WARN] github:{repo} — invalid repository format")
        return []

    url = f"{GITHUB_API}/repos/{repo}/releases/latest"

    try:
        release = fetch_json(url)

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
        print(f"  [WARN] github:{repo} — API error: {exc}")
        return []

    if not isinstance(release, dict):
        print(f"  [WARN] github:{repo} — invalid release response")
        return []

    assets = release.get("assets") or []

    payload_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset_is_payload(asset.get("name", ""))
        and asset.get("browser_download_url")
    ]

    if not payload_assets:
        print(
            f"  [WARN] github:{repo} — "
            "latest release contains no supported payload assets"
        )
        return []

    tag = parse_version(release.get("tag_name", ""))

    release_description = first_release_description(release)

    source = f"https://github.com/{repo}/releases"

    entries: List[dict] = []

    for asset in payload_assets:
        filename = asset["name"]
        direct_url = asset["browser_download_url"]

        name = normalize_payload_name(filename)

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
                "description": description or f"{name} payload",
                "last_update": date_from_iso(
                    release.get("published_at")
                    or release.get("created_at")
                ),
                "version": tag,
                "category": guess_category(name, description),
                "_repo": repo_key(repo),
                "_origin": "github",
                "_manual": bool(manual),
            }
        )

    print(
        f"  [OK] github:{repo} — "
        f"{len(entries)} payload asset(s), {tag}"
    )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Mirror resolver
# ─────────────────────────────────────────────────────────────────────────────

def load_itsplk_mirror_json() -> List[dict]:
    """
    Load the hosted payloads.json from itsPLK's mirror.
    """
    url = (
        "https://itsplk.github.io/"
        "ps5-payloads-mirror/payloads.json"
    )

    try:
        data = fetch_json(url)

    except Exception as exc:
        print(
            f"  [WARN] mirror — "
            f"could not fetch hosted payloads.json: {exc}"
        )
        return []

    if not isinstance(data, list):
        print("  [WARN] mirror — hosted payloads.json is not a list")
        return []

    print(
        f"  [OK] mirror — "
        f"loaded {len(data)} hosted entries"
    )

    return data


def normalize_mirror_entry(raw: dict) -> Optional[dict]:
    """
    Normalize one entry imported from a mirror payloads.json.
    """
    if not isinstance(raw, dict):
        return None

    url = str(raw.get("url") or "").strip()

    if not url:
        return None

    filename = str(
        raw.get("filename")
        or os.path.basename(urlparse(url).path)
        or ""
    ).strip()

    if not asset_is_payload(filename):
        return None

    source = str(raw.get("source") or "").strip()

    repo = github_repo_from_source(source)

    name = str(
        raw.get("name")
        or normalize_payload_name(filename)
    ).strip()

    version = parse_version(
        str(raw.get("version") or "unknown")
    )

    description = clean_description(
        str(raw.get("description") or ""),
        fallback=f"{name} payload",
    )

    source_direct = str(
        raw.get("source_direct")
        or source
        or url
    ).strip()

    entry = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": source or url,
        "source_direct": source_direct,
        "description": description or f"{name} payload",
        "last_update": date_from_iso(
            str(raw.get("last_update") or "")
        ),
        "version": version,
        "category": str(
            raw.get("category")
            or guess_category(name, description)
        ),
        "_repo": repo,
        "_origin": "mirror",
        "_manual": False,
    }

    return entry


def resolve_mirror(spec: str) -> List[dict]:
    """
    Resolve a mirror source.

    The itsPLK mirror gets special handling because it publishes
    a consolidated payloads.json.

    Generic mirrors are read from their specified GitHub release.
    """
    if spec.lower().startswith(
        "itsplk/ps5-payloads-mirror@"
    ):
        raw_entries = load_itsplk_mirror_json()

        entries = []

        for raw in raw_entries:
            entry = normalize_mirror_entry(raw)

            if entry:
                entries.append(entry)

        print(
            f"  [OK] mirror:{spec} — "
            f"{len(entries)} usable entries"
        )

        return entries

    match = re.fullmatch(
        r"([^@]+)@(.+)",
        spec.strip(),
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
        f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    )

    try:
        release = fetch_json(api_url)

    except Exception as exc:
        print(
            f"  [WARN] mirror:{spec} — "
            f"API error: {exc}"
        )
        return []

    if not isinstance(release, dict):
        return []

    entries = []

    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue

        filename = asset.get("name", "")
        direct_url = asset.get("browser_download_url", "")

        if not asset_is_payload(filename) or not direct_url:
            continue

        name = normalize_payload_name(filename)

        entry = {
            "name": name,
            "filename": filename,
            "url": direct_url,
            "source": f"https://github.com/{repo}/releases",
            "source_direct": direct_url,
            "description": f"{name} payload",
            "last_update": date_from_iso(
                release.get("published_at")
                or release.get("created_at")
            ),
            "version": parse_version(
                release.get("tag_name", tag)
            ),
            "category": guess_category(name),
            "_repo": repo_key(repo),
            "_origin": "mirror",
            "_manual": False,
        }

        entries.append(entry)

    print(
        f"  [OK] mirror:{spec} — "
        f"{len(entries)} payload asset(s)"
    )

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Direct URL resolver
# ─────────────────────────────────────────────────────────────────────────────

def resolve_direct(url: str, manual: bool = True) -> List[dict]:
    """
    Resolve a pinned direct payload URL.
    """
    parsed = urlparse(url)

    filename = unquote(
        os.path.basename(parsed.path)
    ).strip()

    if not filename:
        print(f"  [WARN] direct URL has no filename: {url}")
        return []

    if not asset_is_payload(filename):
        print(
            f"  [WARN] unsupported direct payload type: "
            f"{filename}"
        )
        return []

    name = normalize_payload_name(filename)

    entry = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": url,
        "source_direct": url,
        "description": f"{name} payload (pinned)",
        "last_update": datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d"),
        "version": "pinned",
        "category": guess_category(name),
        "_repo": None,
        "_origin": "direct",
        "_manual": bool(manual),
    }

    return [entry]


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication / merging
# ─────────────────────────────────────────────────────────────────────────────

def payload_identity(entry: dict) -> Tuple:
    """
    Build a stable identity for duplicate detection.

    Repository + filename + version is preferred.

    Direct URLs have their URL as identity because there is no
    reliable upstream repository identity.
    """
    repo = entry.get("_repo")
    version = str(entry.get("version") or "").lower()
    filename = str(entry.get("filename") or "").lower()

    if repo:
        return (
            "repo",
            repo,
            version,
            filename,
        )

    return (
        "url",
        str(entry.get("url") or "").lower(),
    )


def same_payload_family(a: dict, b: dict) -> bool:
    """
    Determine whether two entries represent the same payload family.

    This intentionally treats upstream and mirror copies from the
    same repository/release as the same payload.
    """
    repo_a = a.get("_repo")
    repo_b = b.get("_repo")

    if not repo_a or not repo_b:
        return False

    if repo_a != repo_b:
        return False

    version_a = str(a.get("version") or "").lower()
    version_b = str(b.get("version") or "").lower()

    if version_a != version_b:
        return False

    return True


def clean_internal_fields(entry: dict) -> dict:
    """
    Remove internal generator metadata before JSON output.
    """
    return {
        key: value
        for key, value in entry.items()
        if not key.startswith("_")
    }


def merge_entries(
    github_entries: List[dict],
    mirror_entries: List[dict],
    direct_entries: List[dict],
) -> List[dict]:
    """
    Merge all sources using this priority:

      1. Direct pinned URLs
      2. GitHub upstream releases
      3. Mirror fallback

    The mirror is NOT emitted when a usable upstream payload exists
    for the same repository/release.
    """
    result: List[dict] = []

    # Direct URLs are always independent and take priority.
    result.extend(direct_entries)

    # Group upstream entries by repository.
    github_by_repo: Dict[str, List[dict]] = {}

    for entry in github_entries:
        repo = entry.get("_repo")

        if repo:
            github_by_repo.setdefault(repo, []).append(entry)
        else:
            result.append(entry)

    # Add all usable GitHub entries.
    result.extend(github_entries)

    # Mirror entries become fallback entries only when their
    # repository/release is not represented by GitHub.
    for mirror in mirror_entries:
        repo = mirror.get("_repo")

        if repo and repo in github_by_repo:
            github_for_repo = github_by_repo[repo]

            if any(
                same_payload_family(mirror, upstream)
                for upstream in github_for_repo
            ):
                continue

            # If this repository has a usable upstream release,
            # do not duplicate it with the mirror.
            continue

        result.append(mirror)

    # Final identity-based deduplication.
    unique: Dict[Tuple, dict] = {}

    for entry in result:
        identity = payload_identity(entry)

        existing = unique.get(identity)

        if existing is None:
            unique[identity] = entry
            continue

        # Prefer upstream over mirror.
        if (
            existing.get("_origin") == "mirror"
            and entry.get("_origin") == "github"
        ):
            unique[identity] = entry

    return list(unique.values())


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_entries(entries: List[dict]) -> List[dict]:
    """
    Validate all payload URLs.

    A failed upstream URL is discarded here. The mirror fallback has
    already been considered during merging, so a repository with a
    failed upstream asset may still have its mirror entry if the
    upstream repository did not produce a usable entry.
    """
    valid: List[dict] = []

    print(f"\nValidating {len(entries)} payload URL(s)...")

    for entry in entries:
        url = entry.get("url", "")
        name = entry.get("name", "unknown")
        filename = entry.get("filename", "")

        if http_ok(url):
            valid.append(entry)
            print(f"  [OK] {name} — {filename}")
        else:
            print(f"  [SKIP] URL unreachable: {url}")

    return valid


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic ordering
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_ORDER = {
    "System & Jailbreak": 0,
    "Loaders": 1,
    "Networking & Servers": 2,
    "Utilities & Tools": 3,
}


def sort_entries(entries: List[dict]) -> List[dict]:
    """
    Sort payloads deterministically for stable JSON output.
    """
    return sorted(
        entries,
        key=lambda entry: (
            CATEGORY_ORDER.get(
                entry.get("category", ""),
                99,
            ),
            str(entry.get("name", "")).lower(),
            str(entry.get("version", "")).lower(),
            str(entry.get("filename", "")).lower(),
            str(entry.get("url", "")).lower(),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# JSON writing
# ─────────────────────────────────────────────────────────────────────────────

def write_output(entries: List[dict]) -> bool:
    """
    Write payloads.json only when content changed.

    Returns:
        True if the file changed.
        False otherwise.
    """
    public_entries = [
        clean_internal_fields(entry)
        for entry in sort_entries(entries)
    ]

    new_json = json.dumps(
        public_entries,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

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

    temporary_file = OUTPUT_FILE + ".tmp"

    with open(
        temporary_file,
        "w",
        encoding="utf-8",
    ) as file:
        file.write(new_json)

    # Atomic replacement.
    os.replace(temporary_file, OUTPUT_FILE)

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("PLDMGR payload generator")
    print("=" * 72)

    parsed_links = parse_links(LINKS_FILE)

    if not parsed_links:
        print(f"ERROR: {LINKS_FILE} is empty")
        sys.exit(1)

    github_entries: List[dict] = []
    mirror_entries: List[dict] = []
    direct_entries: List[dict] = []

    github_repos_seen = set()
    mirror_specs_seen = set()
    direct_urls_seen = set()

    # ── Resolve sources ────────────────────────────────────────────────

    for value, is_manual in parsed_links:
        print(f"\nProcessing: {value}")

        if value.startswith("github:"):
            repo = value[7:].strip()
            key = repo_key(repo)

            if key in github_repos_seen:
                print(
                    f"  [SKIP] Duplicate GitHub repository: "
                    f"{repo}"
                )
                continue

            github_repos_seen.add(key)

            if is_manual:
                print("  [INFO] Protected manual repository")

            entries = resolve_github(
                repo,
                manual=is_manual,
            )

            github_entries.extend(entries)

        elif value.startswith("mirror:"):
            spec = value[7:].strip()
            key = spec.lower()

            if key in mirror_specs_seen:
                print(
                    f"  [SKIP] Duplicate mirror: {spec}"
                )
                continue

            mirror_specs_seen.add(key)

            entries = resolve_mirror(spec)
            mirror_entries.extend(entries)

        elif (
            value.startswith("http://")
            or value.startswith("https://")
        ):
            key = value.lower()

            if key in direct_urls_seen:
                print(
                    f"  [SKIP] Duplicate direct URL: {value}"
                )
                continue

            direct_urls_seen.add(key)

            entries = resolve_direct(
                value,
                manual=is_manual,
            )

            direct_entries.extend(entries)

        else:
            print(
                f"  [WARN] Unknown format, skipping: {value}"
            )

    print("\n" + "=" * 72)
    print("Resolution summary")
    print("=" * 72)

    print(f"GitHub upstream entries : {len(github_entries)}")
    print(f"Mirror entries           : {len(mirror_entries)}")
    print(f"Direct URL entries       : {len(direct_entries)}")

    # ── Merge ──────────────────────────────────────────────────────────

    merged_entries = merge_entries(
        github_entries=github_entries,
        mirror_entries=mirror_entries,
        direct_entries=direct_entries,
    )

    print(
        f"Merged unique entries   : "
        f"{len(merged_entries)}"
    )

    if not merged_entries:
        print(
            "\nERROR: All entries failed to resolve"
        )
        sys.exit(2)

    # ── Validate ───────────────────────────────────────────────────────

    valid_entries = validate_entries(merged_entries)

    if not valid_entries:
        print(
            "\nERROR: All payload URLs failed validation"
        )
        sys.exit(2)

    # ── Final duplicate protection ─────────────────────────────────────

    final_entries: List[dict] = []
    seen_urls = set()

    for entry in sort_entries(valid_entries):
        url = entry.get("url", "").lower()

        if not url:
            continue

        if url in seen_urls:
            continue

        seen_urls.add(url)
        final_entries.append(entry)

    # ── Write ──────────────────────────────────────────────────────────

    changed = write_output(final_entries)

    print("\n" + "=" * 72)

    if changed:
        print(
            f"Wrote {len(final_entries)} payloads "
            f"to {OUTPUT_FILE}"
        )
    else:
        print(
            f"No changes detected — "
            f"{OUTPUT_FILE} not updated"
        )

    # Category summary.
    category_counts: Dict[str, int] = {}

    for entry in final_entries:
        category = entry.get(
            "category",
            DEFAULT_CATEGORY,
        )

        category_counts[category] = (
            category_counts.get(category, 0) + 1
        )

    print("\nCatalog summary:")

    for category in sorted(
        category_counts,
        key=lambda value: (
            CATEGORY_ORDER.get(value, 99),
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