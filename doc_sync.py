"""Sync sources.json from the Master Links doc.

The doc is the source of truth: every hourly build re-reads the doc's
public HTML export, re-infers the manifest, and merges per-source
fields (password, note, sheet, gid, site_url) from the previous
manifest by matching key. Sources removed from the doc drop out; new
ones appear with the inferred adapter. Any sync failure falls back to
the on-disk manifest (the build never fails on sync).
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request

DOC_ID = "1DL6YCoZLM_ju75DuseNQPtEt3vM4nP_nF2bNqmKtDjo"
DOC_EXPORT_URL = (
    "https://docs.google.com/document/d/" + DOC_ID + "/export?format=html"
)

CLUB_COLLECTION_FOLDER_ID = "1kGaeK1unqWD1VOl3-DGWLAxISBhDR3cg"
CLUB_COLLECTIONS_FOLDER_ID = "1fEaxNbFIrEwKRygETVm4jgtJGVAqKxGB"
CLUB_SITE_URL = "https://sites.google.com/view/faneditfanclub/home"

FIREHOSE_REASON = (
    "Firehose, not a catalog; weekly catalog cron already mines them."
)
SEARCH_TEMPLATE_NOTE = "Query-URL template for later fallback use; not indexed."
BOT_NOTE = "Bot link listed with the search engines; not a query template."
DROPBOX_REASON = (
    "Dropbox folder listing needs an access token; no public listing endpoint."
)
FORM_REASON = "Google Form (request/submission form), not a catalog."
PASTEBIN_NOTE = "Password-protected list; password is in the Master Links doc."

SOURCE_SECTIONS = {"Registries:", "Fan Edit Collections:", "Search Engines:"}

_LINK_RE = re.compile(
    r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE
)
_HEADING_RE = re.compile(
    r"<h([23])[^>]*>(.*?)</h\1>|<li[^>]*>(.*?)</li>|<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw or "")
    # Google Docs headings use non-breaking spaces (&nbsp;); normalize
    # them or section comparisons ("Fan Edit Fan Club Channels:") fail.
    text = html.unescape(text).replace("\xa0", " ")
    return " ".join(text.split())


def unwrap_url(href: str) -> str:
    """Unwrap a Google redirect URL (?q=<real>) and HTML-unescape."""
    parts = urllib.parse.urlparse(href or "")
    if parts.netloc == "www.google.com" and parts.path == "/url":
        qs = urllib.parse.parse_qs(parts.query)
        real = qs.get("q", [href])[0]
        return html.unescape(real)
    return html.unescape(href or "")


def _drive_id(url: str) -> str:
    match = re.search(r"/(?:folders|file/d)/([A-Za-z0-9_-]{10,})", url or "")
    return match.group(1) if match else ""


def _doc_id(url: str, kind: str) -> str:
    # Docs/Sheets links sometimes carry a /u/<n>/ account segment
    # (…/document/u/0/d/<id>/mobilebasic) — allow it.
    pattern = (r"docs\.google\.com/" + kind
               + r"(?:/u/\d+)?/d/([A-Za-z0-9_-]{10,})")
    match = re.search(pattern, url or "")
    return match.group(1) if match else ""


def _paste_id(url: str) -> str:
    match = re.search(r"pastebin\.com/([A-Za-z0-9]+)", url or "")
    if match:
        return match.group(1)
    match = re.search(r"defuse\.ca/b/([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else ""


def _feed_url_for_site(root: str) -> str:
    """RSS feed URL for a blogspot/wordpress site root."""
    parts = urllib.parse.urlparse(root)
    base = parts.scheme + "://" + parts.netloc + parts.path.rstrip("/")
    if "blogspot" in parts.netloc:
        return base + "/feeds/posts/default"
    return base + "/feed/"


def _is_blog_root(url: str) -> bool:
    parts = urllib.parse.urlparse(url or "")
    host = parts.netloc.lower()
    if "blogspot" in host:
        return True
    if "wordpress.com" in host or host.endswith(".wordpress.com"):
        return True
    return False


def parse_doc_entries(html_text: str) -> list[dict]:
    """Parse the doc export into (section, subsection, label, url, password).

    Headings set the section context in document order. Each <li> block
    contributes its first link as the source entry; a "Password:" anchor
    in the same <li> supplies the password (the next anchor's label, or
    the text after "Password:" in the same label). Links outside <li>
    (the header socials) become entries with no password.
    """
    section = ""
    subsection = ""
    entries: list[dict] = []
    for match in _HEADING_RE.finditer(html_text or ""):
        level, heading_raw, li_raw, href, label_raw = match.groups()
        if level:
            text = _clean_text(heading_raw)
            if not text:
                continue
            if level == "2":
                section = text
                subsection = ""
            else:
                subsection = text
            continue
        if li_raw is not None:
            links = [
                (unwrap_url(h), _clean_text(l))
                for h, l in _LINK_RE.findall(li_raw)
            ]
            if not links:
                continue
            url, label = links[0]
            password = ""
            for idx, (_href, text) in enumerate(links[1:], start=1):
                if text.startswith("Password:"):
                    rest = text[len("Password:"):].strip()
                    if rest:
                        password = rest
                    elif idx + 1 < len(links):
                        password = links[idx + 1][1].strip()
                    break
            entries.append({
                "section": section,
                "subsection": subsection,
                "label": label,
                "url": url,
                "password": password,
            })
            continue
        if href:
            entries.append({
                "section": section,
                "subsection": subsection,
                "label": _clean_text(label_raw),
                "url": unwrap_url(href),
                "password": "",
            })
    return entries


def _normalize_url(url: str) -> str:
    """Normalize a URL for dedup: lower host, strip trailing slash and
    tracking params, drop the Drive ?usp=drive_link suffix."""
    parts = urllib.parse.urlparse((url or "").strip())
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k not in ("usp", "utm_source",
             "utm_medium", "utm_name", "utm_term", "utm_content",
             "screen_view_count")]
    query_str = urllib.parse.urlencode(sorted(query))
    return urllib.parse.urlunparse(
        (parts.scheme.lower(), netloc, path, "", query_str, "")
    )


def entry_key(entry: dict) -> str:
    """Stable dedup/merge key: Drive id, sheet id, doc id, paste id,
    or the normalized URL (site_url for RSS roots)."""
    url = entry.get("url") or ""
    for extractor in (_drive_id, lambda u: _doc_id(u, "spreadsheets"),
                      lambda u: _doc_id(u, "document"), _paste_id):
        found = extractor(url)
        if found:
            return found
    site_url = entry.get("site_url") or ""
    if site_url:
        return _normalize_url(site_url)
    return _normalize_url(url)


def infer_source(entry: dict) -> dict:
    """Infer a manifest entry from one doc entry.

    The adapter/type come from the URL pattern with the section as a
    tiebreaker; per-source fields (password, gid, note) are set here
    when the doc itself carries them, and the merge step preserves the
    rest from the old manifest.
    """
    label = (entry.get("label") or "").strip()
    url = (entry.get("url") or "").strip()
    section = entry.get("section") or ""
    password = (entry.get("password") or "").strip()
    host = urllib.parse.urlparse(url).netloc.lower()
    path = urllib.parse.urlparse(url).path

    def out(adapter, stype, **extra):
        item = {"label": label, "url": url, "adapter": adapter, "type": stype}
        item.update(extra)
        return item

    drive_id = _drive_id(url)
    if drive_id in (CLUB_COLLECTION_FOLDER_ID, CLUB_COLLECTIONS_FOLDER_ID):
        return out("drive_folder", "out_of_scope",
                   reason="The club's own collection; the overlap baseline, "
                   "not an external source.")
    if _normalize_url(url) == _normalize_url(CLUB_SITE_URL):
        return out("none", "out_of_scope",
                   reason="The club's own site, not an external source.")
    if section == "Search Engines:":
        if "x.ai" in host:
            return out("none", "search_template", note=BOT_NOTE)
        return out("none", "search_template", note=SEARCH_TEMPLATE_NOTE)
    # Reddit feeds the bot indexes (added 2026-11-09): the club's
    # curated multireddit (what the Reddit poster shares to
    # #reddit-fan-edits-feed) and r/fanedits, as RSS sources. Must
    # come before the club-channel and Reddit-firehose rules below,
    # which would otherwise mark both out of scope.
    _canon = url.split("?")[0].rstrip("/")
    if _canon.lower().endswith("/new"):
        _canon = _canon[:-4].rstrip("/")
    if _canon.lower() == (
            "https://www.reddit.com/user/faneditfanclub/m/"
            "fan_edit_fan_club_reddit_feed"):
        item = out("rss", "source")
        item["url"] = (_canon + "/.rss")
        item["note"] = ("The club's curated multireddit (what the Reddit "
                        "poster automation shares to #reddit-fan-edits-feed).")
        return item
    if _canon.lower() == "https://www.reddit.com/r/fanedits":
        item = out("rss", "source")
        item["url"] = "https://www.reddit.com/r/fanedits/new/.rss"
        item["note"] = "Main fan-edit subreddit."
        return item
    # Any other URL carrying the club's own name is the club's own
    # channel/profile (YouTube, X, Letterboxd, forum profiles, ...),
    # not an external catalog. (Placed after the Search Engines check
    # so the club Reddit feed's search-template entry is preserved.)
    if "faneditfanclub" in url.lower():
        return out("none", "out_of_scope",
                   reason="The club's own channel, not an external source.")
    if "reddit.com" in host and ("/r/" in path or "/m/" in path
                                 or "/user/" in path):
        return out("none", "out_of_scope", reason=FIREHOSE_REASON)
    if host in ("discord.gg", "discord.com") and (
            host == "discord.gg" or path.startswith("/invite")):
        return out("none", "out_of_scope",
                   reason="Discord server, not a catalog.")
    if "facebook.com" in host and path.startswith("/groups/"):
        return out("none", "out_of_scope",
                   reason="Facebook group, not a catalog; weekly catalog "
                   "cron already mines them.")
    if "docs.google.com" in host and "/forms/" in path:
        return out("none", "skipped", reason=FORM_REASON)
    if "dropbox.com" in host:
        return out("none", "skipped", reason=DROPBOX_REASON)
    if drive_id:
        if "/drive/folders/" in url:
            return out("drive_folder", "source")
        return out("drive_file", "source")
    sheet_id = _doc_id(url, "spreadsheets")
    if sheet_id:
        gid = ""
        note = ""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if qs.get("gid"):
            gid = qs["gid"][0]
            note = "Sheet tab selected by gid in URL."
        item = out("sheet", "source")
        if gid:
            item["gid"] = gid
        if note:
            item["note"] = note
        return item
    if _doc_id(url, "document"):
        return out("doc", "source")
    if _paste_id(url):
        item = out("pastebin", "source")
        if password:
            item["password"] = password
            item["note"] = PASTEBIN_NOTE
        return item
    if ("mega" + ".nz") in host:
        return out("mega", "source")
    if ("gofile" + ".io") in host:
        return out("gofile", "source")
    if "yandex" in host:
        return out("yandex", "source")
    if _is_blog_root(url):
        item = out("rss", "source")
        item["url"] = _feed_url_for_site(url)
        item["site_url"] = url
        return item
    if section == "Fan Edit Fan Club Channels:":
        return out("none", "out_of_scope",
                   reason="The club's own channel, not an external source.")
    return out("website", "source")


def sync_sources(old_manifest: list[dict], entries: list[dict]) -> tuple[list[dict], dict]:
    """Regenerate the manifest from doc entries, merging preserved fields.

    Dedup is by (key, section): the same Drive folder in one section
    twice is a duplicate; the same URL in two different sections (the
    fanedit.org search page is a source under Registries and a template
    under Search Engines) is kept twice. Preserved per-source fields
    (password, note, sheet, gid, site_url) carry over from the old
    manifest by key. Returns (new_manifest, report).
    """
    old_by_key: dict[str, dict] = {}
    old_dup_by_key: dict[str, dict] = {}
    for item in old_manifest or []:
        key = entry_key(item)
        old_by_key.setdefault(key, item)
        if item.get("type") == "duplicate":
            old_dup_by_key[key] = item

    new_manifest: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    report = {"added": 0, "removed": 0, "kept": 0, "duplicates": 0,
              "sources_dropped": []}
    old_keys = {entry_key(item) for item in old_manifest or []}
    new_keys: set[str] = set()

    for entry in entries:
        inferred = infer_source(entry)
        key = entry_key(inferred)
        section = entry.get("section") or ""
        new_keys.add(key)
        # Sources dedup by (key, section): the same URL in two sections
        # is two roles (the fanedit.org search page is a source under
        # Registries and a template under Search Engines). Non-sources
        # dedup by (key, type): the club site linked from four sections
        # is one out-of-scope entry, not four.
        if inferred.get("type") == "source":
            dedup_key = (key, section)
        else:
            dedup_key = (key, inferred.get("type"))
        if dedup_key in seen:
            first = seen[dedup_key]
            if inferred.get("type") == "source":
                dup = dict(inferred)
                dup["type"] = "duplicate"
                dup["reason"] = (
                    "Same %s as %s; kept one entry."
                    % ("Drive folder ID" if inferred.get("adapter") == "drive_folder"
                       else "source", first.get("label") or "?")
                )
                # Keep the curated duplicate reason from the old manifest
                # when it names the same entry (e.g. the sort-helper
                # sheet tab over the Central Database sheet). Note: the
                # first entry with this key is the source, so duplicates
                # need their own lookup.
                old_dup = old_dup_by_key.get(key)
                if old_dup is not None and old_dup.get("reason"):
                    dup["reason"] = old_dup["reason"]
                new_manifest.append(dup)
                report["duplicates"] += 1
            continue
        seen[dedup_key] = inferred
        old = old_by_key.get(key)
        if old is not None:
            report["kept"] += 1
            for field in ("password", "note", "sheet", "gid", "site_url"):
                if field in old and field not in inferred:
                    inferred[field] = old[field]
            if entry.get("password"):
                inferred["password"] = entry["password"]
                inferred["note"] = PASTEBIN_NOTE
        else:
            report["added"] += 1
        new_manifest.append(inferred)

    for key in sorted(old_keys - new_keys):
        old = old_by_key.get(key) or {}
        report["removed"] += 1
        if old.get("type") == "source":
            report["sources_dropped"].append(old.get("label") or key)
    return new_manifest, report


def fetch_doc_html(opener=urllib.request.urlopen) -> str:
    req = urllib.request.Request(
        DOC_EXPORT_URL, headers={"User-Agent": "Mozilla/5.0"}
    )
    with opener(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def sync_manifest(old_manifest: list[dict], opener=urllib.request.urlopen):
    """Fetch the doc and regenerate the manifest.

    Returns (new_manifest, report, ok). On any failure returns
    (old_manifest, {"error": ...}, False) so the caller falls back to
    the on-disk manifest.
    """
    try:
        html_text = fetch_doc_html(opener=opener)
        entries = parse_doc_entries(html_text)
        if not entries:
            raise ValueError("doc export parsed to zero entries")
        new_manifest, report = sync_sources(old_manifest, entries)
        return new_manifest, report, True
    except Exception as exc:
        return old_manifest, {"error": "%s: %s" % (type(exc).__name__, exc)}, False
