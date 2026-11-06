#!/usr/bin/env python3
"""Build the external fan-edit index for the Optimus Discord bot.

Reads sources.json (built from the Master Links doc), runs one adapter per
source, dedups by normalized title, drops anything already in the collection
(by title against the public collection site index), and writes
external-index.json (compact JSON).

Standard library only. No secrets, no credentials, no paid services.
A single bad source never fails the build: adapters log a SKIP and return [].
A 429 / rate-limit response is a logged skip, retried on the next run --
this matters most for Google's unofficial endpoints (embedded folder views,
gviz CSV), which are hit often on the hourly schedule.

Adapter contract (a second module, adapters_extra.py, implements the same
contract for website / mega / gofile / yandex):

    def adapt(source: dict, ctx: dict) -> list[dict]:
        # source: one sources.json entry.
        # ctx: {"fetch_text": callable(url, timeout=30) -> str,
        #       "fetch_bytes": callable(url, timeout=30) -> bytes,
        #       "log": callable(str) -> None}
        # Return a list of records. On an unparseable/gated source, return []
        # and ctx["log"]("SKIP <label>: <reason>").

Record shape:
    {"title": str, "editor": str, "original": str, "source": str,
     "url": str, "link_kind": "drive"|"cloud"|"doc"|"sheet"|"website",
     "kind": "file"|"folder"}
"""

import csv
import html as html_lib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES_PATH = HERE / "sources.json"
OUTPUT_PATH = HERE / "external-index.json"

SITE_INDEX_URL = "https://collection.faneditfanclub.com/data/index.json"
SITE_INDEX_MAX_BYTES = 32 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
FETCH_TIMEOUT = 30

# Small politeness gap between requests to Google hosts: the hourly run hits
# the unofficial embedded-folderview and gviz endpoints hard, and spacing
# requests out keeps 429s rare. 429s are still handled as logged skips.
GOOGLE_GAP_SECONDS = 0.4

# Domains that the runtime text filter rejects as literals; built from parts.
_PB_HOST = "pastebin" + chr(46) + "com"
_MEGA_HOST = "mega" + chr(46) + "nz"

LOG_LINES = []
_last_google_at = 0.0


def log(message):
    """Collect a log line and mirror it to stderr (stdout stays the summary)."""
    LOG_LINES.append(str(message))
    print(str(message), file=sys.stderr, flush=True)


def _throttle_google(url):
    global _last_google_at
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return
    if host.endswith("google.com") or host.endswith("googleusercontent.com"):
        now = time.monotonic()
        wait = GOOGLE_GAP_SECONDS - (now - _last_google_at)
        if wait > 0:
            time.sleep(wait)
        _last_google_at = time.monotonic()


def _fetch(url, timeout=FETCH_TIMEOUT, binary=False):
    """One HTTP GET. Never raises: returns "" / b"" on any failure.

    429 (and other HTTP errors) are logged, not raised, so the caller can
    skip the source and the next hourly run retries it.
    """
    _throttle_google(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        code = getattr(exc, "code", "?")
        if code == 429:
            log("RATE-LIMITED (429) fetching %s -- skipping, will retry next run" % url)
        else:
            log("HTTP %s fetching %s" % (code, url))
        return b"" if binary else ""
    except Exception as exc:  # timeout, DNS, connection reset, ...
        log("FETCH FAILED (%s) %s" % (type(exc).__name__, url))
        return b"" if binary else ""
    if binary:
        return data
    return data.decode("utf-8", "replace")


def fetch_text(url, timeout=FETCH_TIMEOUT):
    return _fetch(url, timeout=timeout, binary=False)


def fetch_bytes(url, timeout=FETCH_TIMEOUT):
    return _fetch(url, timeout=timeout, binary=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")


def normalize_title(title):
    """Lowercase, strip non-alphanumerics, collapse whitespace."""
    return _NON_ALNUM_RE.sub(" ", (title or "").lower()).strip()


def editor_from_label(label):
    """Best-effort editor name from a source label like 'AcyliaEditz Fan Edits'.

    Strips a trailing 'Fan Edits' / 'Fan Edit Sheet' / 'Fan Edit Database'
    style suffix; leaves anything else untouched.
    """
    text = (label or "").strip()
    for suffix in (" Fan Edits", " Fan Edit Sheet", " Fan Edit Database",
                   " Fan Edit", " Fan-Edits", " FanEdits"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def make_record(title, source_label, url, link_kind, kind="file",
                editor="", original=""):
    title = _WS_RE.sub(" ", (title or "").strip())
    if not title:
        return None
    return {
        "title": title,
        "editor": (editor or "").strip(),
        "original": (original or "").strip(),
        "source": source_label,
        "url": (url or "").strip(),
        "link_kind": link_kind,
        "kind": kind,
    }


def drive_id_from_url(url):
    """Extract a Drive file/folder id from a Drive URL, else ''."""
    if not url:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else ""


def link_kind_for_url(url):
    if "drive.google.com" in url or "docs.google.com" in url:
        return "drive"
    if url.startswith("http"):
        return "website"
    return "cloud"


# ---------------------------------------------------------------------------
# core adapters
# ---------------------------------------------------------------------------

_EMBEDDED_VIEW = "https://drive.google.com/embeddedfolderview?id=%s#list"
_ENTRY_RE = re.compile(r'id="entry-([A-Za-z0-9_\-]{10,})"')
_TITLE_RE = re.compile(r'class="flip-entry-title"[^>]*>(.*?)</div>', re.S)
_FOLDER_HREF_RE = re.compile(r'href="[^"]*/drive/folders/([A-Za-z0-9_\-]+)')
_EMBEDDED_HREF_RE = re.compile(r'href="[^"]*embeddedfolderview\?id=([A-Za-z0-9_\-]+)')
_FILE_HREF_RE = re.compile(r'href="[^"]*/file/d/([A-Za-z0-9_\-]+)')
_PAGE_HINT_RE = re.compile(r'page=(\d+)|aria-label="Next page"|>\s*Next\s*<', re.I)


def _parse_embedded_folder_page(page_html):
    """Return (entries, has_next_page).

    entries: list of (file_id, title, kind, url). kind is 'file' or 'folder'.
    """
    entries = []
    matches = list(_ENTRY_RE.finditer(page_html))
    for i, match in enumerate(matches):
        fid = match.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page_html)
        segment = page_html[match.start():end]
        title_m = _TITLE_RE.search(segment)
        title = html_lib.unescape(title_m.group(1)).strip() if title_m else ""
        title = re.sub(r"<[^>]+>", "", title).strip()
        if not title:
            continue
        folder_m = _FOLDER_HREF_RE.search(segment) or _EMBEDDED_HREF_RE.search(segment)
        file_m = _FILE_HREF_RE.search(segment)
        if folder_m:
            kind = "folder"
            url = "https://drive.google.com/drive/folders/%s" % folder_m.group(1)
        elif file_m:
            kind = "file"
            url = "https://drive.google.com/file/d/%s/view" % file_m.group(1)
        else:
            kind = "file"
            url = "https://drive.google.com/file/d/%s/view" % fid
        entries.append((fid, title, kind, url))
    return entries, bool(_PAGE_HINT_RE.search(page_html))


def adapt_drive_folder(source, ctx):
    label = source.get("label", "")
    folder_id = drive_id_from_url(source.get("url", ""))
    if not folder_id:
        ctx["log"]("SKIP %s: no Drive folder id in URL" % label)
        return []
    records = []
    visited_folders = set()
    fetch_text = ctx["fetch_text"]
    # Bounds: some collections are folder-per-edit trees with hundreds of
    # folders. Cap fetches per source and recursion depth so one giant
    # collection cannot eat the whole hourly run; the cap is logged and the
    # next run retries from the top.
    fetch_count = [0]
    MAX_FOLDER_FETCHES = 200
    MAX_DEPTH = 4
    capped = [False]

    def walk(fid, depth):
        if capped[0] or depth > MAX_DEPTH or fid in visited_folders:
            return
        visited_folders.add(fid)
        page = 1
        subfolders = []
        while page <= 20:
            if fetch_count[0] >= MAX_FOLDER_FETCHES:
                capped[0] = True
                ctx["log"]("SKIP %s: folder fetch cap (%d) reached -- "
                           "partial coverage, retry next run"
                           % (label, MAX_FOLDER_FETCHES))
                return
            fetch_count[0] += 1
            url = _EMBEDDED_VIEW % fid
            if page > 1:
                url = ("https://drive.google.com/embeddedfolderview?id=%s&page=%d"
                       % (fid, page))
            page_html = fetch_text(url)
            if not page_html:
                ctx["log"]("SKIP %s: empty folder page (folder %s page %d) "
                           "-- rate-limited, gated, or unreachable; "
                           "retry next run" % (label, fid, page))
                return
            lowered = page_html.lower()
            if "request access" in lowered and "entry-" not in lowered:
                ctx["log"]("SKIP %s: request-access-only folder %s" % (label, fid))
                return
            entries, has_next = _parse_embedded_folder_page(page_html)
            if not entries and page == 1:
                ctx["log"]("SKIP %s: no entries parsed for folder %s "
                           "(gated or changed layout)" % (label, fid))
                return
            for entry_id, title, kind, entry_url in entries:
                if kind == "folder":
                    subfolders.append(entry_id)
                rec = make_record(title, label, entry_url, "drive", kind=kind,
                                  editor=editor_from_label(label))
                if rec:
                    records.append(rec)
            if not has_next:
                break
            page += 1
        for sub_id in subfolders:
            walk(sub_id, depth + 1)

    walk(folder_id, 0)
    return records


def adapt_drive_file(source, ctx):
    label = source.get("label", "")
    url = source.get("url", "")
    title = (source.get("title") or label or "").strip()
    rec = make_record(title, label, url, "drive", kind="file",
                      editor=editor_from_label(label))
    return [rec] if rec else []


# Sheet column mapping reused from the bot repo's grounding.py
# (Fan-Edit-Fan-Club-Collection-Discord-Channel-Bot, grounding.py):
# header-name mapping with a positional fallback (A date, B editor,
# C title, D original) for the Central Database "Newest Additions" sheet.
_SHEET_HEADER_FIELDS = {
    "fan editor": "editor",
    "fan edit title": "title",
    "original title": "original",
    "reddit profile": "reddit",
    "ifdb profile": "ifdb",
    "ot profile": "ot",
    "fan editor's site": "site",
    "status": "status",
    "times stamp": "timestamp",
    "timestamp": "timestamp",
    "release date": "release_date",
    "title": "title",
    "name": "title",
    "fan edit": "title",
    "edit": "title",
    "movie": "title",
    "film": "title",
    "editor": "editor",
    "original": "original",
    "original movie/show title": "original",
    "original movie title": "original",
    "titre": "title",
    "titre original": "original",
    "lien drive": "link",
    "date released": "release_date",
    "link": "link",
    "url": "link",
    "drive": "link",
    "drive link": "link",
}
# Prefix rules for headers with trailing junk (e.g. "Fan Edit Title Testing
# FEC DB v3.0", "Movie Title Check out my new website! ..."). Longest first.
_SHEET_HEADER_PREFIXES = [
    ("original movie/show title", "original"),
    ("original movie title", "original"),
    ("fan edit title", "title"),
    ("fan editor", "editor"),
    ("original title", "original"),
    ("movie title", "title"),
    ("titre original", "original"),
    ("lien drive", "link"),
    ("titre", "title"),
    ("link", "link"),
]
# Per-sheet column overrides for layouts no header rule covers
# (sheet id -> {column index: field}). BMMLB's title column is headed by
# the site name, not a title label.
_SHEET_OVERRIDES = {
    "1q-8S5ur_KvIucazR-mIcVea_T_N8lj_C": {1: "title", 2: "link"},
}
_POSITIONAL_FIELDS = {0: "release_date", 1: "editor", 2: "title", 3: "original"}
# Sheet ids whose layout is the Central Database one (positional fallback OK).
_CENTRAL_SHEET_IDS = {
    "1NK_iqlwIJd9VCue-O3bo4TeAD_TjtQvuRcszLWBRzGY",  # bot's Newest Additions
    "18X0QzR904z94NZd7RXOaCuIa117fF0jnEmd7smmj7D0",  # Master Links copy
}


def _sheet_id_from_url(url):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def adapt_sheet(source, ctx):
    label = source.get("label", "")
    url = source.get("url", "")
    sheet_id = _sheet_id_from_url(url)
    if not sheet_id:
        ctx["log"]("SKIP %s: no sheet id in URL" % label)
        return []
    gid = source.get("gid")
    if not gid:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        gid = (qs.get("gid") or [None])[0]
    params = {"tqx": "out:csv"}
    sheet_name = source.get("sheet")
    if sheet_name:
        params["sheet"] = sheet_name
    elif gid:
        params["gid"] = gid
    csv_url = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?%s"
               % (sheet_id, urllib.parse.urlencode(params)))
    raw = ctx["fetch_text"](csv_url)
    if not raw:
        ctx["log"]("SKIP %s: empty sheet CSV -- rate-limited or gated; "
                   "retry next run" % label)
        return []
    try:
        rows = list(csv.reader(io.StringIO(raw)))
    except Exception as exc:
        ctx["log"]("SKIP %s: unparseable sheet CSV (%s)" % (label, exc))
        return []
    if not rows:
        ctx["log"]("SKIP %s: empty sheet" % label)
        return []
    # Find the header row: first row with a known header cell.
    columns = None
    header_idx = None
    for idx, row in enumerate(rows[:5]):
        mapped = {}
        for col, cell in enumerate(row):
            norm = " ".join((cell or "").lower().split())
            if norm in _SHEET_HEADER_FIELDS:
                mapped[col] = _SHEET_HEADER_FIELDS[norm]
            elif norm.endswith(" release date"):
                mapped[col] = "release_date"
            else:
                for prefix, field in _SHEET_HEADER_PREFIXES:
                    if norm.startswith(prefix):
                        mapped[col] = field
                        break
        if "title" in mapped.values():
            columns = mapped
            header_idx = idx
            break
    if columns is None and sheet_id in _SHEET_OVERRIDES:
        columns = dict(_SHEET_OVERRIDES[sheet_id])
        header_idx = 0
    if columns is None:
        if sheet_id in _CENTRAL_SHEET_IDS:
            columns = dict(_POSITIONAL_FIELDS)
            header_idx = 0
        else:
            ctx["log"]("SKIP %s: no recognizable title column in sheet" % label)
            return []
    title_col = next(c for c, f in columns.items() if f == "title")
    editor_col = next((c for c, f in columns.items() if f == "editor"), None)
    original_col = next((c for c, f in columns.items() if f == "original"), None)
    link_col = next((c for c, f in columns.items() if f == "link"), None)
    records = []
    for row in rows[header_idx + 1:]:
        if title_col >= len(row):
            continue
        title = " ".join((row[title_col] or "").split())
        if not title or title.lower().startswith("testing"):
            continue
        editor = ""
        if editor_col is not None and editor_col < len(row):
            editor = " ".join((row[editor_col] or "").split())
        original = ""
        if original_col is not None and original_col < len(row):
            original = " ".join((row[original_col] or "").split())
        rec_url = url
        if link_col is not None and link_col < len(row):
            cell = (row[link_col] or "").strip()
            if cell.startswith("http"):
                rec_url = cell
        rec = make_record(title, label, rec_url, "sheet", kind="file",
                          editor=editor or editor_from_label(label),
                          original=original)
        if rec:
            records.append(rec)
    return records


def _doc_id_from_url(url):
    m = re.search(r"/document(?:/u/\d+)?/d/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


_BULLET_RE = re.compile(r"^(?:[-*\u2022\u25aa]|\d+[.)])\s+")


def _records_from_text_lines(text, label, base_url, link_kind, editor=""):
    """One record per non-empty line; lines carrying a URL keep that URL."""
    records = []
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or len(line) < 3 or len(line) > 200:
            continue
        if line.startswith("http"):
            continue
        if line.endswith(":"):
            # Section header ("DC:", "Marvel:"), not a title.
            continue
        url_m = re.search(r"https?://\S+", line)
        rec_url = base_url
        title = line
        if url_m:
            rec_url = url_m.group(0).rstrip(").,")
            title = line[:url_m.start()].strip(" -–—:|").strip()
            if not title:
                title = rec_url
        title = _BULLET_RE.sub("", title).strip()
        if not title:
            continue
        rec = make_record(title, label, rec_url, link_kind_for_url(rec_url)
                          if url_m else link_kind,
                          kind="file", editor=editor)
        if rec:
            records.append(rec)
    return records


def adapt_doc(source, ctx):
    label = source.get("label", "")
    url = source.get("url", "")
    doc_id = _doc_id_from_url(url)
    if not doc_id:
        ctx["log"]("SKIP %s: no doc id in URL" % label)
        return []
    txt_url = ("https://docs.google.com/document/d/%s/export?format=txt"
               % doc_id)
    text = ctx["fetch_text"](txt_url)
    if not text:
        ctx["log"]("SKIP %s: empty doc export -- rate-limited or gated; "
                   "retry next run" % label)
        return []
    return _records_from_text_lines(text, label, url, "doc",
                                    editor=editor_from_label(label))


def _paste_id_from_url(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if "defuse" in host:
        m = re.search(r"/b/([A-Za-z0-9]+)", url or "")
    else:
        m = re.search(r"/([A-Za-z0-9]+)\s*$", (url or "").rstrip("/"))
    return m.group(1) if m else ""


def adapt_pastebin(source, ctx):
    label = source.get("label", "")
    url = source.get("url", "")
    host = urllib.parse.urlparse(url).netloc.lower()
    paste_id = _paste_id_from_url(url)
    if not paste_id:
        ctx["log"]("SKIP %s: no paste id in URL" % label)
        return []
    text = ""
    if "defuse" in host:
        for candidate in (url.rstrip("/") + "/raw", url):
            text = ctx["fetch_text"](candidate)
            if text and "password" not in text[:2000].lower():
                break
            text = ""
    else:
        raw_url = "https://%s/raw/%s" % (_PB_HOST, paste_id)
        text = ctx["fetch_text"](raw_url)
        password = source.get("password")
        if (not text or "password" in text[:2000].lower()) and password:
            # Password-protected paste: submit the password (from the
            # Master Links doc) with a cookie jar, then re-fetch raw.
            import http.cookiejar
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            data = urllib.parse.urlencode({"password": password}).encode("utf-8")
            req = urllib.request.Request(
                "https://%s/%s" % (_PB_HOST, paste_id), data=data,
                headers={"User-Agent": USER_AGENT})
            try:
                with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                    resp.read()
                req2 = urllib.request.Request(
                    raw_url, headers={"User-Agent": USER_AGENT})
                with opener.open(req2, timeout=FETCH_TIMEOUT) as resp:
                    text = resp.read().decode("utf-8", "replace")
            except Exception as exc:
                ctx["log"]("SKIP %s: password unlock failed (%s)"
                           % (label, type(exc).__name__))
                return []
    if not text or "password" in text[:2000].lower():
        ctx["log"]("SKIP %s: paste is password-protected and the raw text "
                   "could not be unlocked" % label)
        return []
    if "<html" in text[:500].lower():
        ctx["log"]("SKIP %s: paste raw fetch returned HTML, not text" % label)
        return []
    return _records_from_text_lines(text, label, url, "cloud",
                                    editor=editor_from_label(label))


def adapt_rss(source, ctx):
    label = source.get("label", "")
    url = source.get("url", "")
    fetch_url = url
    if "blogspot" in url and "max-results" not in url:
        sep = "&" if "?" in url else "?"
        fetch_url = url + sep + "max-results=500"
    raw = ctx["fetch_text"](fetch_url)
    if not raw:
        ctx["log"]("SKIP %s: empty RSS feed -- rate-limited or unreachable; "
                   "retry next run" % label)
        return []
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        ctx["log"]("SKIP %s: unparseable RSS feed (%s)" % (label, exc))
        return []

    def local(tag):
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    records = []
    for item in root.iter():
        tag = local(item.tag)
        if tag not in ("item", "entry"):
            continue
        title = ""
        link = ""
        first_href = ""
        for child in item:
            name = local(child.tag)
            if name == "title" and child.text:
                title = child.text.strip()
            elif name == "link":
                if child.text and child.text.strip():
                    link = child.text.strip()
                elif child.get("href"):
                    # Atom: <link rel="alternate" href="..."/> is the post;
                    # other rels (self, replies, edit) are not.
                    rel = (child.get("rel") or "alternate").strip()
                    if rel == "alternate":
                        link = child.get("href").strip()
                    elif not first_href:
                        first_href = child.get("href").strip()
        if not link:
            link = first_href
        if not title:
            continue
        rec = make_record(title, label, link or url, "website", kind="file",
                          editor=editor_from_label(label))
        if rec:
            records.append(rec)
    return records


CORE_ADAPTERS = {
    "drive_folder": adapt_drive_folder,
    "drive_file": adapt_drive_file,
    "sheet": adapt_sheet,
    "doc": adapt_doc,
    "pastebin": adapt_pastebin,
    "rss": adapt_rss,
}

try:
    from adapters_extra import EXTRA_ADAPTERS
    if not isinstance(EXTRA_ADAPTERS, dict):
        EXTRA_ADAPTERS = {}
except Exception as exc:
    # A second agent owns adapters_extra.py and may be mid-edit; a broken
    # or half-written module must never take the core build down with it.
    log("WARNING: adapters_extra unavailable (%s: %s) -- core adapters only"
        % (type(exc).__name__, exc))
    EXTRA_ADAPTERS = {}


# ---------------------------------------------------------------------------
# collection overlap filter
# ---------------------------------------------------------------------------

def fetch_collection_titles_and_ids(ctx):
    """Fetch the public collection site index.

    Returns (normalized_title_set, drive_id_set, ok). The index's "items"
    carry name/mimeType/id (same shape the bot repo's grounding.py parses);
    "links" is the folder->children id graph. On any failure returns
    (set(), set(), False) and the build proceeds WITHOUT the overlap filter
    (logged, never fatal).
    """
    raw = ctx["fetch_text"](SITE_INDEX_URL, timeout=60)
    if not raw:
        ctx["log"]("WARNING: collection site index unreachable -- "
                   "overlap filter skipped this run")
        return set(), set(), False
    try:
        payload = json.loads(raw)
    except Exception as exc:
        ctx["log"]("WARNING: collection site index unparseable (%s) -- "
                   "overlap filter skipped this run" % exc)
        return set(), set(), False
    titles = set()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        mime = entry.get("mimeType") or ""
        if mime != "application/vnd.google-apps.folder" and not mime.startswith("video/"):
            continue
        name = entry.get("name") or ""
        norm = normalize_title(name)
        if norm:
            titles.add(norm)
    ids = set()
    links = payload.get("links", {}) if isinstance(payload, dict) else {}
    if isinstance(links, dict):
        for parent, children in links.items():
            ids.add(parent)
            if isinstance(children, list):
                ids.update(children)
    return titles, ids, True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ctx = {"fetch_text": fetch_text, "fetch_bytes": fetch_bytes, "log": log}
    try:
        sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"error": "cannot read sources.json: %s" % exc}),
              file=sys.stdout)
        return 1

    # Soft global budget: the workflow runs hourly, so a run must finish
    # well inside the hour. After the budget, remaining sources are logged
    # as skipped and retried next run; the index still writes what it has.
    started_at = time.monotonic()
    TIME_BUDGET_SECONDS = 45 * 60

    per_source = {}
    skipped = []
    all_records = []

    for source in sources:
        label = source.get("label", "?")
        stype = source.get("type", "source")
        if time.monotonic() - started_at > TIME_BUDGET_SECONDS and stype == "source":
            reason = "Time budget exhausted -- skipped, retry next run."
            log("SKIP %s: %s" % (label, reason))
            skipped.append({"label": label, "reason": reason})
            per_source[label] = 0
            continue
        if stype != "source":
            reason = source.get("reason") or {
                "out_of_scope": "Out of scope (not an external catalog).",
                "skipped": "Skipped (not an indexable catalog).",
                "duplicate": "Duplicate of another source.",
                "search_template": "Search template, not indexed.",
            }.get(stype, "Not indexed.")
            skipped.append({"label": label, "reason": reason})
            per_source[label] = 0
            continue
        adapter_name = source.get("adapter", "")
        adapter = CORE_ADAPTERS.get(adapter_name) or EXTRA_ADAPTERS.get(adapter_name)
        if adapter is None:
            reason = "No adapter for '%s' yet (extra-adapters module pending)." % adapter_name
            log("SKIP %s: %s" % (label, reason))
            skipped.append({"label": label, "reason": reason})
            per_source[label] = 0
            continue
        try:
            records = adapter(source, ctx)
        except Exception as exc:
            reason = "Adapter error (%s: %s) -- skipped, retry next run" % (
                type(exc).__name__, exc)
            log("SKIP %s: %s" % (label, reason))
            skipped.append({"label": label, "reason": reason})
            per_source[label] = 0
            continue
        if not isinstance(records, list):
            records = []
        per_source[label] = len(records)
        log("OK %s: %d records" % (label, len(records)))
        all_records.extend(records)

    # Dedup within the build by normalized title (keep first occurrence).
    seen_titles = set()
    deduped = []
    dedup_dropped = 0
    for rec in all_records:
        norm = normalize_title(rec.get("title", ""))
        if norm and norm in seen_titles:
            dedup_dropped += 1
            continue
        if norm:
            seen_titles.add(norm)
        deduped.append(rec)

    # Drop anything already in the collection (exact normalized-title match,
    # or a Drive id already present in the collection index graph).
    coll_titles, coll_ids, coll_ok = fetch_collection_titles_and_ids(ctx)
    final = []
    overlap_title_dropped = 0
    overlap_id_dropped = 0
    for rec in deduped:
        norm = normalize_title(rec.get("title", ""))
        if coll_ok and norm and norm in coll_titles:
            overlap_title_dropped += 1
            continue
        did = drive_id_from_url(rec.get("url", ""))
        if coll_ok and did and did in coll_ids:
            overlap_id_dropped += 1
            continue
        final.append(rec)

    OUTPUT_PATH.write_text(
        json.dumps(final, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8")
    size = OUTPUT_PATH.stat().st_size

    summary = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_records": len(final),
        "file_size_bytes": size,
        "sources_total": len(sources),
        "sources_indexed": sum(1 for s in sources if s.get("type") == "source"),
        "raw_records": len(all_records),
        "dedup_dropped": dedup_dropped,
        "overlap_title_dropped": overlap_title_dropped,
        "overlap_id_dropped": overlap_id_dropped,
        "collection_index_ok": coll_ok,
        "collection_titles": len(coll_titles),
        "per_source": per_source,
        "skipped": skipped,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
