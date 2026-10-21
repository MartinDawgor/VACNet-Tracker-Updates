import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TARGET_CLIPS_URL = "https://www.counter-strike.net/vacnet/clips"
TARGET_PORTAL_URL = "https://www.counter-strike.net/vacnet"
TARGET_INVITE_URL = "https://www.counter-strike.net/vacnet/createinvite"

REPO_ROOT = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parent.parent))
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
CURRENT_DIR = SNAPSHOTS_DIR / "current"
OUTDATED_DIR = SNAPSHOTS_DIR / "outdated"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
USER_AGENT = "VACnetTracker/2026 (+https://github.com/vacnet-tracker)"
REQUEST_TIMEOUT = (10, 45)
MAX_ASSET_BYTES = 8 * 1024 * 1024
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

IGNORED_TAGS = {"script", "style", "noscript"}
RUNTIME_CLASSES = re.compile(
    r"^(?:vjs-(?:has-started|paused|user-active|play-locked|metamodel|vol-[01]|hidden|workinghover|controls-enabled)|video-dimensions)$"
)
RUNTIME_IDS = re.compile(r"^vjs-[\w-]+-\d+$")
RUNTIME_SELECTORS = (
    ".video-glow",
    ".vjs-start-message",
    ".vjs-poster",
    ".vjs-title-bar",
    ".vjs-text-track-display",
    ".vjs-loading-spinner",
    ".vjs-big-play-button",
    ".vjs-control-bar",
    ".vjs-watched-progress",
    ".vjs-cheating-marker",
)
DYNAMIC_ATTRIBUTES = {"checked", "selected", "disabled", "autoplay", "muted"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_url(value: str, base_url: str = TARGET_CLIPS_URL) -> str:
    absolute = urljoin(base_url, value)
    parts = urlsplit(absolute)
    netloc = re.sub(r"^cdn\.(?:fastly|akamai|cloudflare)\.steamstatic\.com$", "cdn.[CDN].steamstatic.com", parts.netloc)
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key not in {"submit_success", "_cdn", "v"}]
    path = re.sub(r"/[^/]+_files(?=/|$)", "/[ASSET_DIR]", parts.path)
    return urlunsplit((parts.scheme, netloc, path, urlencode(query), ""))


def request_url(value: str, base_url: str = TARGET_CLIPS_URL) -> str:
    absolute = urljoin(base_url, value)
    parts = urlsplit(absolute)
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key not in {"submit_success"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def normalize_text(value: str, context: str = "") -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"https?://cdn\.(?:fastly|akamai|cloudflare)\.steamstatic\.com", "https://cdn.[CDN].steamstatic.com", value)
    value = re.sub(r"https://www\.counter-strike\.net/vacnet/join\?invite=[\w-]+", "https://www.counter-strike.net/vacnet/join?invite=[INVITE_TOKEN]", value)
    value = re.sub(r"\b[a-z0-9]{8}-[A-Z0-9]{16}\b", "[INVITE_TOKEN]", value)
    if context in {"clip", "source"}:
        value = re.sub(r"(?i)(/cs_overwatch/cl_)[0-9a-f]+(\.webm)", r"\1[CLIP_HASH]\2", value)
    if context in {"task", "url"}:
        value = re.sub(r"(?<=/vacnet/view\?s=)[^&\s\"']+", "[TASK_TOKEN]", value)
        value = re.sub(r"\b\d{5,}\b", "[TASK_ID]", value)
    if context == "invite_intro":
        return "[INVITE_INTRO_TEXT]"
    return value


def normalize_attribute(name: str, value: Any, tag_name: str) -> str:
    if name in DYNAMIC_ATTRIBUTES:
        return "[STATE]"
    text = " ".join(value) if isinstance(value, list) else str(value)
    if name in {"src", "poster"}:
        return re.sub(r"(?i)(/cs_overwatch/cl_)[0-9a-f]+(\.webm)", r"\1[CLIP_HASH]\2", canonical_url(text))
    if name == "href":
        text = canonical_url(text)
        if "/oauth/logout" in text or "/vacnet/login" in text:
            return "[AUTH_ENDPOINT]"
        if "join?invite=" in text:
            return "https://www.counter-strike.net/vacnet/join?invite=[INVITE_TOKEN]"
        if "createinvite" in text:
            return "[INVITE_ENDPOINT]"
    if name == "style":
        text = re.sub(r"--video-ratio\s*:\s*[^;]+", "--video-ratio:[RATIO]", text)
        text = re.sub(r"(?:inset|margin|width|height|left|top)\s*:\s*[^;]+", "[RUNTIME_STYLE]", text)
        text = re.sub(r"https?://cdn\.(?:fastly|akamai|cloudflare)\.steamstatic\.com", "https://cdn.[CDN].steamstatic.com", text)
    if name in {"id", "for", "aria-labelledby"} and (RUNTIME_IDS.fullmatch(text) or re.search(r"(?:^|[-_])\d+$", text) and text.startswith("vjs")):
        return "[RUNTIME_ID]"
    if name == "class":
        text = " ".join(sorted(token for token in text.split() if not RUNTIME_CLASSES.fullmatch(token)))
    if name == "value" and tag_name == "input":
        text = "[DYNAMIC_VALUE]"
    if name.startswith("data-"):
        text = normalize_text(text, "task" if "task" in name else "")
    return normalize_text(text, "url" if name == "href" else "")


def is_runtime_node(tag: Tag) -> bool:
    if tag.name in {"canvas"}:
        return True
    classes = set(tag.get("class", []))
    element_id = tag.get("id", "")
    return any(
        (selector.startswith(".") and selector[1:] in classes)
        or (selector.startswith("#") and selector[1:] == element_id)
        for selector in RUNTIME_SELECTORS
    )


def is_dynamic_text_context(tag: Tag) -> bool:
    classes = set(tag.get("class", []))
    return bool(tag.get("id") == "submithint" or classes.intersection({"submithint"}))


def normalize_dom(element: Tag) -> dict[str, Any]:
    node: dict[str, Any] = {"tag": element.name}
    attrs = {}
    for name in sorted(element.attrs):
        if name in {"aria-live", "aria-disabled"} or name.startswith("on"):
            continue
        attrs[name] = normalize_attribute(name, element.attrs[name], element.name)
    if "invitebutton" in element.get("class", []):
        attrs["href"] = "[INVITE_ENDPOINT]"
    if element.get("id") == "copyinvitebutton":
        attrs["disabled"] = "[STATE]"
    if attrs:
        node["attrs"] = attrs
    children = []
    for child in element.children:
        if isinstance(child, Comment) or child is None:
            continue
        if isinstance(child, NavigableString):
            parent_classes = set(element.get("class", []))
            context = ""
            if "intro-text-p" in parent_classes and ("invite" in element.text.lower() or "приглаш" in element.text.lower()):
                context = "invite_intro"
            elif "invite-link-p" in parent_classes:
                context = "url"
            elif element.name in {"a", "td", "input"}:
                context = "task"
            text = normalize_text(str(child), context)
            if is_dynamic_text_context(element):
                text = "[DYNAMIC_TEXT]"
            if element.get("id") == "copyinvitebutton":
                text = "[INVITE_BUTTON_TEXT]"
            parent_classes = set(element.parent.get("class", [])) if isinstance(element.parent, Tag) else set()
            if {"ClipCount", "panel-clipcount"}.intersection(parent_classes | set(element.get("class", []))):
                text = re.sub(r"\b\d+\b", "[CLIP_COUNT]", text)
            if text:
                children.append({"text": "[CONTENT]" if element.name in {"body", "html"} else text})
        elif isinstance(child, Tag) and child.name not in IGNORED_TAGS and not is_runtime_node(child):
            children.append(normalize_dom(child))
    if children:
        node["children"] = children
    return node


def normalize_inline_content(content: str) -> str:
    content = re.sub(r"(?m)<!--.*?-->", "", content, flags=re.DOTALL)
    content = re.sub(r"(?i)(/cs_overwatch/cl_)[0-9a-f]+(\.webm)", r"\1[CLIP_HASH]\2", content)
    content = re.sub(r"\b(?:startTime|endTime|eventTime|eventTimeInFullVideo)\s*=\s*[^;]+", "[TIME_VALUE]", content)
    content = re.sub(r"(eventTimeInFullVideo\s*=\s*)[^;]+", r"\1[TIME_VALUE]", content)
    content = re.sub(r"(return\s+)[^;]+(\s*\+\s*\(?\s*secondsFromEvent)", r"\1[TICK_BASE]\2", content)
    content = re.sub(r'const inviteStr = "[^"]+"', 'const inviteStr = "[INVITE_TOKEN]"', content)
    content = re.sub(r"https://www\.counter-strike\.net/vacnet/join\?invite=[\w-]+", "https://www.counter-strike.net/vacnet/join?invite=[INVITE_TOKEN]", content)
    content = re.sub(r"\b[a-z0-9]{8}-[A-Z0-9]{16}\b", "[INVITE_TOKEN]", content)
    content = re.sub(r"https?://cdn\.(?:fastly|akamai|cloudflare)\.steamstatic\.com", "https://cdn.[CDN].steamstatic.com", content)
    content = re.sub(r"\s+", " ", content).strip()
    return content


def normalize_asset_bytes(content: bytes, url: str) -> bytes:
    path_lower = urlsplit(url).path.lower()
    if path_lower.endswith((".css", ".js", ".svg")):
        text = content.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"https?://cdn\.(?:fastly|akamai|cloudflare)\.steamstatic\.com", "https://cdn.[CDN].steamstatic.com", text)
        text = re.sub(r"([?&])_cdn=(?:fastly|akamai|cloudflare)(&|$)", r"\1\2", text)
        return text.encode("utf-8")
    return content


def normalize_html_content(html: str) -> str:
    text = html.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"https?://cdn\.(?:fastly|akamai|cloudflare)\.steamstatic\.com", "https://cdn.[CDN].steamstatic.com", text)
    text = re.sub(r"([?&])_cdn=(?:fastly|akamai|cloudflare)(&|$)", r"\1\2", text)
    text = re.sub(r"(\?|&amp;|&)_cdn=(?:fastly|akamai|cloudflare)", "", text)
    text = re.sub(r"(?<=/vacnet/view\?s=)[a-f0-9]{32,}", "[TASK_TOKEN]", text)
    text = re.sub(r"\b[a-z0-9]{8}-[A-Z0-9]{16}\b", "[INVITE_TOKEN]", text)
    text = re.sub(r"https://www\.counter-strike\.net/vacnet/join\?invite=[\w-]+", "https://www.counter-strike.net/vacnet/join?invite=[INVITE_TOKEN]", text)
    return text


def asset_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    values = []
    for tag in soup.find_all(["script", "link", "img"]):
        raw = tag.get("src") if tag.name in {"script", "img"} else tag.get("href")
        if not raw:
            continue
        url = request_url(raw, page_url)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.path.lower().endswith((".js", ".css", ".svg")):
            values.append(url)
            if "novtt" in parsed.path.lower():
                values.append("https://vjs.zencdn.net/vttjs/0.14.1/vtt.min.js")
    return sorted(set(values))


def detect_variant(soup: BeautifulSoup) -> str:
    video = soup.find("video", id="video")
    classes = video.get("class", []) if video else []
    if "vjs-metamodel" in classes:
        return "full_match"
    inputs = [inp.get("value", "") for inp in soup.find_all("input", attrs={"name": "verdict_labels[]"})]
    if any("cheating" in val for val in inputs) and not any("aimassist" in val for val in inputs):
        return "full_match"
    return "short_clip"


def target_dir(name: str) -> Path:
    return CURRENT_DIR / name


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=0.5, status_forcelist=sorted(RETRYABLE_STATUS_CODES), allowed_methods={"GET"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    return session


def fetch_page(session: requests.Session, url: str, cookie: str = "", require_auth: bool = False) -> tuple[str, str]:
    headers = {"Cookie": cookie} if cookie else {}
    response = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if require_auth and (len(response.content) < 1000 or "please log in" in response.text.lower()):
        raise RuntimeError("authentication failed: login page received")
    return response.text, response.url


def fetch_asset(session: requests.Session, url: str) -> dict[str, Any]:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        if response.status_code in RETRYABLE_STATUS_CODES:
            response.raise_for_status()
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_ASSET_BYTES:
                raise ValueError("asset exceeds size limit")
            chunks.append(chunk)
        raw_content = b"".join(chunks)
        content = normalize_asset_bytes(raw_content, url)
        return {"url": canonical_url(url), "status": response.status_code, "sha256": sha256_bytes(content), "bytes": len(content), "content": content}
    except (requests.RequestException, ValueError) as error:
        return {"url": canonical_url(url), "status": "error", "error": str(error)}


def parse_page(html: str, page_url: str, session: requests.Session, is_clips: bool = False) -> tuple[dict[str, Any], dict[str, bytes]]:
    soup = BeautifulSoup(html, "lxml")
    variant = detect_variant(soup) if is_clips else ""
    assets = []
    assets_content: dict[str, bytes] = {}
    for url in asset_urls(soup, page_url):
        result = fetch_asset(session, url)
        content = result.pop("content", None)
        if content is not None:
            filename = Path(urlsplit(url).path).name or (hashlib.sha256(url.encode()).hexdigest()[:12] + ".bin")
            assets_content[filename] = content
        assets.append(result)
    inline = []
    for script in soup.find_all(["script", "style"]):
        if script.get("src") or not script.string or not script.string.strip():
            continue
        normalized = normalize_inline_content(script.string)
        inline.append({"tag": script.name, "sha256": sha256_bytes(normalized.encode()), "bytes": len(normalized)})
    body = soup.body
    parsed = {
        "schema_version": 2,
        "variant": variant,
        "_meta": {"url": canonical_url(page_url), "fetched_at": datetime.now(timezone.utc).isoformat(), "html_bytes": len(html.encode()), "html_sha256": sha256_bytes(html.encode())},
        "assets": [{key: value for key, value in item.items() if key != "error"} for item in assets],
        "asset_errors": [{"url": item["url"], "error": item["error"]} for item in assets if item.get("status") == "error"],
        "inline_resources": inline,
        "dom": normalize_dom(body) if body else {},
    }
    return parsed, assets_content


def comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(snapshot))
    value.pop("_meta", None)
    value.pop("asset_errors", None)
    return value


def compare_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
    old_value, new_value = comparable(old), comparable(new)
    if old_value == new_value:
        return None
    sections = [key for key in ("assets", "inline_resources", "dom") if old_value.get(key) != new_value.get(key)]
    return {"detected_at": datetime.now(timezone.utc).isoformat(), "sections_changed": sections, "details": {key: {"old": old_value.get(key), "new": new_value.get(key)} for key in sections}, "asset_errors": new.get("asset_errors", [])}


def save_snapshot(html: str, parsed: dict[str, Any], name: str, assets_content: dict[str, bytes]) -> None:
    target = target_dir(name)
    target.mkdir(parents=True, exist_ok=True)
    assets_dir = target / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for filename, data in assets_content.items():
        (assets_dir / filename).write_bytes(data)
    clean_html = normalize_html_content(html)
    (target / "page.html").write_text(clean_html, encoding="utf-8")
    (target / "parsed.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def archive_snapshot(name: str, timestamp: str) -> Path:
    archive_dir = OUTDATED_DIR / f"{timestamp}_{name}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    source = target_dir(name)
    if source.exists():
        shutil.copytree(source, archive_dir, dirs_exist_ok=True)
    return archive_dir


def load_snapshot(name: str) -> tuple[str | None, dict[str, Any] | None]:
    target = target_dir(name)
    html_path = target / "page.html"
    json_path = target / "parsed.json"
    if html_path.exists() and json_path.exists():
        return html_path.read_text(encoding="utf-8"), json.loads(json_path.read_text(encoding="utf-8"))
    return None, None


def write_output(key: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def write_summary(value: str) -> None:
    output = os.environ.get("GITHUB_STEP_SUMMARY")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(value + "\n")


def update_changelog(changes: dict[str, Any], timestamp: str, target: str) -> None:
    sections = ", ".join(f"`{item}`" for item in changes.get("sections_changed", [])) or "unknown"
    entry = f"\n## [{timestamp}] — Target: `{target}`\n\n**Changes detected**\n\n**Sections changed:** {sections}\n\n---\n"
    existing = CHANGELOG_PATH.read_text(encoding="utf-8") if CHANGELOG_PATH.exists() else "# VACnet Site Tracker — Changelog\n\n"
    CHANGELOG_PATH.write_text(existing + entry, encoding="utf-8")


def main() -> int:
    cookie = os.environ.get("STEAM_COOKIE", "").strip()
    if not cookie:
        print("::error::STEAM_COOKIE environment variable is not set.", file=sys.stderr)
        return 2
    session = create_session()

    try:
        html, page_url = fetch_page(session, TARGET_CLIPS_URL, cookie=cookie, require_auth=True)
        parsed, assets_content = parse_page(html, page_url, session, is_clips=True)
    except (requests.RequestException, RuntimeError, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2

    variant = parsed["variant"]
    v_assets = target_dir(variant) / "assets"
    v_assets.mkdir(parents=True, exist_ok=True)
    for filename, data in assets_content.items():
        (v_assets / filename).write_bytes(data)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    changed_targets = []
    all_sections = []
    primary_archive_dir = ""

    old_html, old_parsed = load_snapshot(variant)
    if old_parsed is None:
        save_snapshot(html, parsed, variant, assets_content)
        write_output("first_run", "true")
    else:
        changes = compare_snapshots(old_parsed, parsed)
        if changes is not None:
            archive_dir = archive_snapshot(variant, timestamp)
            primary_archive_dir = str(archive_dir.relative_to(REPO_ROOT)).replace("\\", "/")
            save_snapshot(html, parsed, variant, assets_content)
            if old_html is not None:
                clean_old = normalize_html_content(old_html)
                clean_new = normalize_html_content(html)
                diff = unified_diff(clean_old.splitlines(True), clean_new.splitlines(True), fromfile=f"previous/{variant}/page.html", tofile=f"current/{variant}/page.html")
                (archive_dir / "changes.diff").write_text("".join(diff), encoding="utf-8")
            (archive_dir / "changes.json").write_text(json.dumps(changes, indent=2, ensure_ascii=False), encoding="utf-8")
            update_changelog(changes, timestamp, variant)
            changed_targets.append(variant)
            all_sections.extend([f"{variant}:{s}" for s in changes["sections_changed"]])

    extra_targets = {
        "portal": TARGET_PORTAL_URL,
        "createinvite": TARGET_INVITE_URL,
    }
    for t_name, t_url in extra_targets.items():
        try:
            anon_session = create_session()
            e_html, e_url = fetch_page(anon_session, t_url, cookie="", require_auth=False)
            e_parsed, e_assets = parse_page(e_html, e_url, anon_session, is_clips=False)
            e_old_html, e_old_parsed = load_snapshot(t_name)
            if e_old_parsed is None:
                save_snapshot(e_html, e_parsed, t_name, e_assets)
            else:
                e_changes = compare_snapshots(e_old_parsed, e_parsed)
                if e_changes is not None:
                    e_archive_dir = archive_snapshot(t_name, timestamp)
                    if not primary_archive_dir:
                        primary_archive_dir = str(e_archive_dir.relative_to(REPO_ROOT)).replace("\\", "/")
                    save_snapshot(e_html, e_parsed, t_name, e_assets)
                    if e_old_html is not None:
                        clean_old = normalize_html_content(e_old_html)
                        clean_new = normalize_html_content(e_html)
                        e_diff = unified_diff(clean_old.splitlines(True), clean_new.splitlines(True), fromfile=f"previous/{t_name}/page.html", tofile=f"current/{t_name}/page.html")
                        (e_archive_dir / "changes.diff").write_text("".join(e_diff), encoding="utf-8")
                    (e_archive_dir / "changes.json").write_text(json.dumps(e_changes, indent=2, ensure_ascii=False), encoding="utf-8")
                    update_changelog(e_changes, timestamp, t_name)
                    changed_targets.append(t_name)
                    all_sections.extend([f"{t_name}:{s}" for s in e_changes["sections_changed"]])
        except Exception as error:
            print(f"::warning::Failed to track {t_name}: {error}", file=sys.stderr)

    write_output("timestamp", timestamp)
    write_output("variant", variant)

    if changed_targets:
        sections_str = ", ".join(all_sections)
        write_output("changed", "true")
        write_output("sections", sections_str)
        write_output("archive_dir", primary_archive_dir)
        write_output("issue_title", f"VACnet site update detected ({', '.join(changed_targets)}) — {timestamp}")
        write_summary(f"## VACnet Tracker\n\nChanges detected in: {', '.join(changed_targets)}\n\nSections: {sections_str}")
        return 1

    if os.environ.get("FORCE_SNAPSHOT", "false").lower() == "true":
        save_snapshot(html, parsed, variant, assets_content)
        write_output("force_snapshot", "true")

    write_output("changed", "false")
    write_summary(f"## VACnet Tracker\n\nNo structural or asset changes detected for `{variant}`, `portal`, and `createinvite`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
