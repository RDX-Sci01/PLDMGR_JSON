#!/usr/bin/env python3
"""
generate.py — builds payloads.json from links.txt

Exit codes:
  0 — success, payloads.json written or unchanged
  1 — links.txt is empty or missing
  2 — all entries failed to resolve or validate
"""

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

ASSET_EXTENSIONS = (".elf", ".bin", ".lua")

OUTPUT_FILE = "payloads.json"
LINKS_FILE = "links.txt"

REQUEST_TIMEOUT = 15
HEAD_TIMEOUT = 10
GET_TIMEOUT = 15

USER_AGENT = "payloads-generator/1.0"

# Category guessing keywords
CATEGORY_RULES = [
    (
        ["jailbreak", "kstuff", "etahen", "hen", "exploit", "lapy", "daemon"],
        "System & Jailbreak",
    ),
    (
        [
            "ftp",
            "http",
            "web",
            "server",
            "srv",
            "dns",
            "upload",
            "network",
            "telnet",
            "shell",
        ],
        "Networking & Servers",
    ),
    (
        ["loader", "elfldr", "linux", "autoloader", "webkit"],
        "Loaders",
    ),
]

DEFAULT_CATEGORY = "Utilities & Tools"


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def gh_headers():
    """Return headers used for GitHub API requests."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def generic_headers():
    """Return headers used for non-GitHub requests."""
    return {
        "User-Agent": USER_AGENT,
    }


def fetch_json(url, github=False):
    """Fetch and decode a JSON response."""
    headers = gh_headers() if github else generic_headers()

    req = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            data = response.read().decode("utf-8")
            return json.loads(data)

    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code} {exc.reason}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"connection error: {exc.reason}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON response: {exc}"
        ) from exc


def normalize_url(url):
    """
    Normalize a URL for duplicate detection.

    This does not attempt to rewrite the URL in the generated output.
    """
    if not isinstance(url, str):
        return ""

    url = url.strip()

    if not url:
        return ""

    try:
        parsed = urllib.parse.urlsplit(url)

        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()

        if not scheme or not hostname:
            return ""

        port = parsed.port

        # Rebuild authority while preserving non-default ports.
        authority = hostname

        if port is not None:
            default_port = (
                (scheme == "http" and port == 80)
                or (scheme == "https" and port == 443)
            )

            if not default_port:
                authority = f"{authority}:{port}"

        normalized = urllib.parse.urlunsplit(
            (
                scheme,
                authority,
                parsed.path,
                parsed.query,
                "",
            )
        )

        # Only remove trailing slash when the path isn't simply "/".
        if parsed.path not in ("", "/") and normalized.endswith("/"):
            normalized = normalized.rstrip("/")

        return normalized

    except ValueError:
        return ""


def valid_http_url(url):
    """Return True if URL is a valid HTTP(S) URL."""
    try:
        parsed = urllib.parse.urlsplit(url)

        if parsed.scheme.lower() not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        return True

    except ValueError:
        return False


def head_ok(url):
    """
    Validate a URL.

    HEAD is attempted first because it avoids downloading the payload.
    Some servers/CDNs reject HEAD, so a small GET fallback is used.
    """
    if not valid_http_url(url):
        return False

    # First attempt: HEAD.
    try:
        req = urllib.request.Request(
            url,
            headers=generic_headers(),
            method="HEAD",
        )

        with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT) as response:
            return response.status < 400

    except urllib.error.HTTPError as exc:
        # 405/501 commonly mean HEAD isn't supported.
        if exc.code not in (405, 501):
            # Still try GET below because some CDNs behave inconsistently.
            pass

    except Exception:
        pass

    # Fallback: GET only a tiny amount of data.
    try:
        req = urllib.request.Request(
            url,
            headers={
                **generic_headers(),
                "Range": "bytes=0-0",
            },
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=GET_TIMEOUT) as response:
            response.read(1)
            return response.status < 400

    except Exception:
        return False


# ── General helpers ──────────────────────────────────────────────────────────

def guess_category(name, description=""):
    text = f"{name} {description}".lower()

    for keywords, category in CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category

    return DEFAULT_CATEGORY


def parse_version(tag):
    """Return a clean version string."""
    if not tag:
        return "unknown"

    tag = str(tag).strip()

    if not tag:
        return "unknown"

    return tag


def asset_is_payload(name):
    """Return True for supported payload extensions."""
    if not isinstance(name, str):
        return False

    return name.lower().endswith(ASSET_EXTENSIONS)


def date_from_iso(value):
    """
    Convert an ISO timestamp/date into YYYY-MM-DD.

    Returns 'unknown' when a value is present but malformed.
    """
    if not value:
        return "unknown"

    value = str(value).strip()

    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)

    if match:
        return match.group(1)

    return "unknown"


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def clean_description(value, fallback):
    """
    Extract a concise first useful line from a description.
    """
    if not isinstance(value, str):
        return fallback

    for line in value.splitlines():
        line = line.strip()

        if not line:
            continue

        # Remove common Markdown heading markers.
        line = re.sub(r"^#+\s*", "", line)

        # Collapse whitespace.
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            return line[:160]

    return fallback


def clean_payload_name(filename):
    """
    Generate a stable display name from a payload filename.

    Examples:
      ftp.elf             -> ftp
      ftp-v1.2.3.elf      -> ftp
      ftp_1.2.3.bin       -> ftp
      my-payload.elf      -> my-payload
    """
    if not isinstance(filename, str):
        return "unknown"

    base = os.path.basename(filename)

    stem, _ = os.path.splitext(base)

    # Remove common version suffixes:
    #   -v1.2
    #   _v1.2
    #   -1.2
    #   _1.2
    #   -v2026.01
    # etc.
    stem = re.sub(
        r"(?:[_-]+)v?\d+(?:[._-]\d+)*$",
        "",
        stem,
        flags=re.IGNORECASE,
    )

    # Clean whitespace.
    stem = re.sub(r"\s+", " ", stem).strip()

    return stem or base


def normalize_entry(entry):
    """
    Validate and normalize a payload entry.

    Returns the normalized entry or None.
    """
    if not isinstance(entry, dict):
        return None

    name = str(entry.get("name") or "").strip()
    filename = str(entry.get("filename") or "").strip()
    url = str(entry.get("url") or "").strip()

    if not name:
        name = clean_payload_name(filename)

    if not filename:
        try:
            filename = os.path.basename(
                urllib.parse.urlsplit(url).path
            )
        except Exception:
            filename = ""

    if not url or not valid_http_url(url):
        return None

    if not filename:
        return None

    if not asset_is_payload(filename):
        return None

    description = clean_description(
        entry.get("description"),
        f"{name} payload",
    )

    normalized = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": str(entry.get("source") or url),
        "source_direct": str(entry.get("source_direct") or url),
        "description": description,
        "last_update": date_from_iso(entry.get("last_update")),
        "version": parse_version(entry.get("version")),
        "category": str(
            entry.get("category")
            or guess_category(name, description)
        ),
    }

    return normalized


# ── GitHub resolvers ─────────────────────────────────────────────────────────

def resolve_github(user_repo):
    """Fetch the latest GitHub release from a repository."""
    user_repo = user_repo.strip().strip("/")

    if not re.match(r"^[^/\s]+/[^/\s]+$", user_repo):
        print(
            f"  [WARN] github:{user_repo} — "
            f"invalid repository format, expected user/repo"
        )
        return []

    url = f"{GITHUB_API}/repos/{user_repo}/releases/latest"

    try:
        rel = fetch_json(url, github=True)

    except Exception as exc:
        print(
            f"  [WARN] github:{user_repo} — API error: {exc}"
        )
        return []

    if not isinstance(rel, dict):
        print(
            f"  [WARN] github:{user_repo} — "
            f"unexpected API response"
        )
        return []

    assets = [
        asset
        for asset in rel.get("assets", [])
        if isinstance(asset, dict)
        and asset_is_payload(asset.get("name", ""))
        and asset.get("browser_download_url")
    ]

    if not assets:
        print(
            f"  [WARN] github:{user_repo} — "
            f"no .elf/.bin/.lua in latest release"
        )
        return []

    # Deterministic asset selection.
    #
    # Prefer names that explicitly look like a payload and avoid
    # obvious checksums/signatures/debug variants.
    def asset_score(asset):
        name = asset["name"].lower()

        score = 0

        if name.endswith(".elf"):
            score += 30
        elif name.endswith(".bin"):
            score += 20
        elif name.endswith(".lua"):
            score += 10

        if "payload" in name:
            score += 20

        if "ps5" in name:
            score += 10

        if any(
            bad in name
            for bad in (
                "sha256",
                "checksum",
                ".sig",
                "signature",
                "debug",
                "symbols",
            )
        ):
            score -= 100

        return (-score, name)

    assets.sort(key=asset_score)
    asset = assets[0]

    tag = rel.get("tag_name", "")
    repo_name = user_repo.split("/")[-1]

    description = clean_description(
        rel.get("body"),
        f"{repo_name} payload",
    )

    entry = {
        "name": repo_name,
        "filename": asset["name"],
        "url": asset["browser_download_url"],
        "source": f"https://github.com/{user_repo}/releases",
        "source_direct": asset["browser_download_url"],
        "description": description,
        "last_update": date_from_iso(rel.get("published_at")),
        "version": parse_version(tag),
        "category": guess_category(repo_name, description),
    }

    entry = normalize_entry(entry)

    if not entry:
        print(
            f"  [WARN] github:{user_repo} — "
            f"resolved asset failed validation"
        )
        return []

    print(
        f"  [OK] github:{user_repo} — "
        f"{entry['filename']}"
    )

    return [entry]


def resolve_mirror(user_repo_tag):
    """
    Resolve a mirror source.

    Supported format:
        mirror:user/repo@tag

    Special handling:
        itsPLK/ps5-payloads-mirror

    For the itsPLK mirror, payloads.json from GitHub Pages is
    preferred because it represents the mirror's published index.
    """
    user_repo_tag = user_repo_tag.strip()

    # Special published mirror.
    if (
        user_repo_tag.startswith("itsPLK/ps5-payloads-mirror")
    ):
        try:
            entries = fetch_json(
                "https://itsplk.github.io/"
                "ps5-payloads-mirror/payloads.json"
            )

            if not isinstance(entries, list):
                raise RuntimeError(
                    "payloads.json is not a JSON array"
                )

            normalized_entries = []

            for raw_entry in entries:
                entry = normalize_entry(raw_entry)

                if entry:
                    normalized_entries.append(entry)

            print(
                f"  [OK] mirror — imported "
                f"{len(normalized_entries)} valid entries "
                f"from itsPLK payloads.json"
            )

            return normalized_entries

        except Exception as exc:
            print(
                f"  [WARN] mirror — could not fetch "
                f"itsPLK payloads.json: {exc}; "
                f"falling back to GitHub API"
            )

    match = re.match(
        r"^([^@/\s]+/[^@/\s]+)@(.+)$",
        user_repo_tag,
    )

    if not match:
        print(
            f"  [WARN] mirror:{user_repo_tag} — "
            f"invalid format, expected user/repo@tag"
        )
        return []

    user_repo = match.group(1)
    tag = match.group(2)

    url = (
        f"{GITHUB_API}/repos/{user_repo}/"
        f"releases/tags/{urllib.parse.quote(tag, safe='')}"
    )

    try:
        rel = fetch_json(url, github=True)

    except Exception as exc:
        print(
            f"  [WARN] mirror:{user_repo_tag} — "
            f"API error: {exc}"
        )
        return []

    if not isinstance(rel, dict):
        print(
            f"  [WARN] mirror:{user_repo_tag} — "
            f"unexpected API response"
        )
        return []

    entries = []

    for asset in rel.get("assets", []):
        if not isinstance(asset, dict):
            continue

        filename = asset.get("name", "")
        download_url = asset.get("browser_download_url", "")

        if not asset_is_payload(filename):
            continue

        if not download_url:
            continue

        name = clean_payload_name(filename)

        entry = {
            "name": name,
            "filename": filename,
            "url": download_url,
            "source": f"https://github.com/{user_repo}/releases",
            "source_direct": download_url,
            "description": f"{name} payload",
            "last_update": date_from_iso(
                rel.get("published_at")
            ),
            "version": parse_version(tag),
            "category": guess_category(name),
        }

        entry = normalize_entry(entry)

        if entry:
            entries.append(entry)

    # Deterministic ordering.
    entries.sort(
        key=lambda item: (
            item["name"].lower(),
            item["filename"].lower(),
        )
    )

    print(
        f"  [OK] mirror:{user_repo_tag} — "
        f"imported {len(entries)} assets"
    )

    return entries


def resolve_direct(url):
    """Resolve a pinned direct payload URL."""
    url = url.strip()

    if not valid_http_url(url):
        print(
            f"  [WARN] direct URL is invalid: {url}"
        )
        return []

    try:
        parsed = urllib.parse.urlsplit(url)
        filename = os.path.basename(parsed.path)
    except Exception:
        filename = ""

    if not filename:
        print(
            f"  [WARN] direct URL has no filename: {url}"
        )
        return []

    if not asset_is_payload(filename):
        print(
            f"  [WARN] unsupported payload extension: "
            f"{filename}"
        )
        return []

    name = clean_payload_name(filename)

    entry = {
        "name": name,
        "filename": filename,
        "url": url,
        "source": url,
        "source_direct": url,
        "description": f"{name} payload (pinned)",
        "last_update": today_utc(),
        "version": "pinned",
        "category": guess_category(name),
    }

    entry = normalize_entry(entry)

    return [entry] if entry else []


# ── Input handling ───────────────────────────────────────────────────────────
def parse_links(path):
    lines = []
    seen = set()

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            for raw_line in file:
                line = raw_line.strip()

                if not line or line.startswith("#"):
                    continue

                key = line.casefold()

                if key in seen:
                    print(
                        f"  [SKIP] Duplicate entry "
                        f"in links.txt: {line}"
                    )
                    continue

                seen.add(key)
                lines.append(line)

# ── Entry deduplication ──────────────────────────────────────────────────────
def deduplicate_entries(entries):
    """
    Deduplicate entries by normalized download URL first,
    then by name — catches mirror + github: overlap where
    the same payload appears under different URLs.

    When two entries share a name, the one from a direct
    github: resolve is preferred over a mirror import
    (direct = more up-to-date version/URL).
    """
    # Pass 1: deduplicate by URL
    url_seen = set()
    url_deduped = []
    for entry in entries:
        key = normalize_url(entry.get("url", ""))
        if not key:
            continue
        if key in url_seen:
            print(f"  [SKIP] Duplicate URL: {entry.get('url')}")
            continue
        url_seen.add(key)
        url_deduped.append(entry)

    # Pass 2: deduplicate by name, prefer direct github: entries
    # (identified by source_direct == url, not a mirror domain)
    name_seen = {}  # lower_name -> index in result
    result = []
    for entry in url_deduped:
        name_key = entry.get("name", "").strip().lower()
        if not name_key:
            result.append(entry)
            continue

        is_direct = (
            "github.com" in entry.get("source", "")
            and entry.get("url") == entry.get("source_direct")
        )

        if name_key not in name_seen:
            name_seen[name_key] = len(result)
            result.append(entry)
        else:
            existing_idx = name_seen[name_key]
            existing = result[existing_idx]
            existing_is_direct = (
                "github.com" in existing.get("source", "")
                and existing.get("url") == existing.get("source_direct")
            )
            if is_direct and not existing_is_direct:
                # Replace mirror entry with direct entry
                print(f"  [DEDUP] Preferring direct source for: {entry['name']}")
                result[existing_idx] = entry
            else:
                print(f"  [SKIP] Duplicate name: {entry['name']}")

    return result


def sort_entries(entries):
    """Return entries in deterministic order."""
    return sorted(
        entries,
        key=lambda entry: (
            str(entry.get("category", "")).casefold(),
            str(entry.get("name", "")).casefold(),
            str(entry.get("filename", "")).casefold(),
            str(entry.get("url", "")).casefold(),
        ),
    )


# ── Output handling ─────────────────────────────────────────────────────────

def load_existing_output(path):
    """Read the existing output file if present."""
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
        print(
            f"  [WARN] Could not read existing "
            f"{path}: {exc}"
        )
        return None


def atomic_write(path, content):
    """
    Atomically replace the output file.

    The temporary file is created in the same directory so os.replace()
    remains atomic on the same filesystem.
    """
    directory = os.path.dirname(os.path.abspath(path))
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

        os.replace(temp_path, path)
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
    lines = parse_links(LINKS_FILE)

    if not lines:
        print(
            f"ERROR: {LINKS_FILE} is empty"
        )
        sys.exit(1)

    all_entries = []

    for line in lines:
        print(f"Processing: {line}")

        if line.startswith("github:"):
            entries = resolve_github(line[7:])

        elif line.startswith("mirror:"):
            entries = resolve_mirror(line[7:])

        elif (
            line.startswith("http://")
            or line.startswith("https://")
        ):
            entries = resolve_direct(line)

        else:
            print(
                f"  [WARN] Unknown format, skipping: "
                f"{line}"
            )
            continue

        all_entries.extend(entries)

    # Normalize everything once more after resolution.
    normalized_entries = []

    for entry in all_entries:
        normalized = normalize_entry(entry)

        if normalized:
            normalized_entries.append(normalized)

    all_entries = deduplicate_entries(normalized_entries)

    if not all_entries:
        print(
            "ERROR: All entries failed to resolve"
        )
        sys.exit(2)

    # Deterministic ordering before validation/output.
    all_entries = sort_entries(all_entries)

    # Validate URLs.
    print(
        f"\nValidating {len(all_entries)} URLs..."
    )

    valid_entries = []

    for entry in all_entries:
        url = entry.get("url", "")

        print(
            f"  Checking: {entry['name']} "
            f"({url})"
        )

        if head_ok(url):
            valid_entries.append(entry)
            print("    [OK]")
        else:
            print(
                f"    [SKIP] URL unreachable: "
                f"{url}"
            )

    if not valid_entries:
        print(
            "ERROR: All URLs failed validation"
        )
        sys.exit(2)

    valid_entries = sort_entries(valid_entries)

    # Generate JSON.
    new_json = json.dumps(
        valid_entries,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    # Validate generated JSON before touching the existing file.
    try:
        parsed_output = json.loads(new_json)

        if not isinstance(parsed_output, list):
            raise ValueError(
                "generated JSON is not an array"
            )

    except Exception as exc:
        print(
            f"ERROR: Generated JSON failed validation: "
            f"{exc}"
        )
        sys.exit(2)

    # Only write if content actually changed.
    old_json = load_existing_output(OUTPUT_FILE)

    if (
        old_json is not None
        and old_json.strip() == new_json.strip()
    ):
        print(
            f"\nNo changes detected — "
            f"{OUTPUT_FILE} not updated"
        )
        sys.exit(0)

    try:
        atomic_write(
            OUTPUT_FILE,
            new_json,
        )

    except OSError as exc:
        print(
            f"ERROR: Could not write "
            f"{OUTPUT_FILE}: {exc}"
        )
        sys.exit(2)

    print(
        f"\nWrote {len(valid_entries)} payloads "
        f"to {OUTPUT_FILE}"
    )

    sys.exit(0)


if __name__ == "__main__":
    main()