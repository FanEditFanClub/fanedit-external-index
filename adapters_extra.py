"""adapters_extra.py — long-tail adapters for the Optimus external-search index.

Covers the sources the core builder does not: the ~41 editor/personal
websites from the Master Links doc plus the public Mega / GoFile / Yandex
cloud links. Imported by the main builder (build.py) as::

    from adapters_extra import EXTRA_ADAPTERS

Contract (exact — do not change the names or shapes)::

    EXTRA_ADAPTERS = {"website": adapt_website, "mega": adapt_mega,
                      "gofile": adapt_gofile, "yandex": adapt_yandex}

    def adapt(source: dict, ctx: dict) -> list[dict]:
        # source: one sources.json entry
        #         {"label": str, "url": str, "adapter": str, ...}
        # ctx: {"fetch_text": callable(url, timeout=30) -> str,
        #       "fetch_bytes": callable(url, timeout=30) -> bytes,
        #       "log": callable(str) -> None}
        # Returns a list of records. On an unparseable / gated source,
        # returns [] and logs "SKIP <label>: <reason>".

Record shape (exact)::

    {"title": str, "editor": str, "original": str, "source": label,
     "url": str, "link_kind": "drive"|"cloud"|"doc"|"sheet"|"website",
     "kind": "file"|"folder"}

``link_kind`` here is "website" for the website adapter and "cloud" for
mega / gofile / yandex. ``editor`` / ``original`` are "" when unknown.

Stdlib only (urllib + html.parser + xml parsers). No secrets, no keys,
no accounts, no paid services.

RATE-LIMIT POLICY — the builder now runs HOURLY (24x/day), so every
adapter treats a 429 / rate-limit response as skip-and-retry-next-run:
it logs the skip and returns [], NEVER raises and NEVER fails the build.
This applies to the website fetches and to the Mega / GoFile / Yandex
APIs alike. Every fetch in this module is wrapped so that *any*
exception (429, 403, 404, 5xx, timeout, parse error) degrades to []
plus a log line. Per-source fetch budget is capped (MAX_FETCHES_PER_SOURCE)
so one hourly run stays polite.

Notes on the cloud adapters:
- Mega: anonymous public-folder API (POST g.api.mega.co.nz/cs, command
  "f"). Node names are AES-encrypted, so this module carries a small
  pure-Python AES-128 (validated against the FIPS-197 test vector).
  No account, no key beyond the one embedded in the public link URL.
- GoFile: guest token from POST api.gofile.io/accounts (free, anonymous)
  then GET api.gofile.io/contents/<id>. As of 2026-11-05 the contents
  endpoint answers "error-notPremium" to guest tokens, so public GoFile
  folders are currently NOT listable anonymously; the adapter still
  attempts the flow and logs the skip.
- Yandex: public resources API, fully anonymous, no token.

The cloud adapters use urllib directly (rather than ctx fetchers) because
they need POST bodies / Authorization headers, which the ctx fetchers do
not accept. The website adapter uses the ctx fetchers.
"""

import base64
import html as _html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as _ET
from html.parser import HTMLParser

__all__ = ["EXTRA_ADAPTERS", "adapt", "adapt_website", "adapt_mega",
           "adapt_gofile", "adapt_yandex"]

_UA = "Mozilla/5.0 (compatible; OptimusExternalIndex/1.0; +https://faneditfanclub.com)"

# Hard cap on HTTP fetches per source per run (hourly builder: stay polite).
MAX_FETCHES_PER_SOURCE = 4
# Hard cap on records emitted per source per run.
MAX_RECORDS_PER_SOURCE = 500


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def _log(ctx, msg):
    try:
        fn = ctx.get("log") if isinstance(ctx, dict) else None
        if callable(fn):
            fn(msg)
    except Exception:
        pass


def _is_rate_limit(exc):
    """True when an exception (or a status code) signals HTTP 429 / rate limiting."""
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status", None)
    if code is not None:
        try:
            if int(code) == 429:
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc).lower()
    return ("429" in msg or "rate limit" in msg or "rate-limit" in msg
            or "rate_limited" in msg or "too many requests" in msg
            or "slow down" in msg)


class _State:
    """Per-adapt-call fetch bookkeeping."""

    def __init__(self):
        self.fetches = 0
        self.rate_limited = False


def _fetch_text(ctx, state, url, label, timeout=30, quiet=False):
    """ctx fetch_text wrapper. Returns str or None. Never raises.

    On a 429 / rate-limit signal it logs the rate-limit skip (unless
    quiet, in which case the caller logs it once) and sets
    state.rate_limited so the caller stops trying further URLs for this
    source (retry next hourly run instead of hammering).
    """
    if state is not None:
        if state.rate_limited or state.fetches >= MAX_FETCHES_PER_SOURCE:
            return None
        state.fetches += 1
    try:
        fn = ctx.get("fetch_text") if isinstance(ctx, dict) else None
        if not callable(fn):
            return None
        text = fn(url, timeout=timeout)
        if text is None:
            return None
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return text
    except Exception as exc:  # noqa: BLE001 - every failure degrades to skip
        if _is_rate_limit(exc):
            if state is not None:
                state.rate_limited = True
            if not quiet:
                _log(ctx, "SKIP %s: rate-limited (HTTP 429) on %s — retry next run"
                     % (label, url))
        elif not quiet:
            _log(ctx, "SKIP %s: fetch failed for %s: %s"
                 % (label, url, _short_err(exc)))
        return None


def _fetch_bytes_direct(url, label, ctx, state, data=None, headers=None,
                        timeout=30):
    """Direct urllib fetch (needed for POST / Authorization headers).

    Returns bytes or None. Never raises; 429 is logged as rate-limited.
    """
    if state is not None:
        if state.rate_limited or state.fetches >= MAX_FETCHES_PER_SOURCE:
            return None
        state.fetches += 1
    hdrs = {"User-Agent": _UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or _is_rate_limit(exc):
            if state is not None:
                state.rate_limited = True
            _log(ctx, "SKIP %s: rate-limited (HTTP 429) on %s — retry next run"
                 % (label, url))
        else:
            body = b""
            try:
                body = exc.read()[:200]
            except Exception:
                pass
            _log(ctx, "SKIP %s: HTTP %s on %s %s"
                 % (label, exc.code, url, body.decode("utf-8", "replace")))
        return None
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit(exc):
            if state is not None:
                state.rate_limited = True
            _log(ctx, "SKIP %s: rate-limited on %s — retry next run"
                 % (label, url))
        else:
            _log(ctx, "SKIP %s: fetch failed for %s: %s"
                 % (label, url, _short_err(exc)))
        return None


def _short_err(exc):
    return str(exc)[:160].replace("\n", " ")


def _clean(text):
    if not text:
        return ""
    text = _html.unescape(str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _record(source_label, editor, title, url, link_kind, kind="file",
            original=""):
    return {
        "title": _clean(title),
        "editor": _clean(editor),
        "original": _clean(original),
        "source": source_label,
        "url": url,
        "link_kind": link_kind,
        "kind": kind,
    }


def _dedupe(records):
    seen = set()
    out = []
    for rec in records:
        key = (rec["title"].lower(), rec["url"])
        if key in seen or not rec["title"]:
            continue
        seen.add(key)
        out.append(rec)
        if len(out) >= MAX_RECORDS_PER_SOURCE:
            break
    return out


def _editor_from_source(source):
    if source.get("editor"):
        return source["editor"]
    label = (source.get("label") or "").strip()
    name = label
    # "X's FAN-EDIT Compendium" -> "X"
    m = re.match(r"^(.+?)'s\s+(FAN-EDIT|Fan Edit|Fan-Edit)", label, re.I)
    if m:
        return m.group(1).strip()
    name = re.sub(r"\s+(Fan Edits?|Fan-Edits?|FanEdit.*|Edits|Fan Edit Fan Club.*)$",
                  "", name, flags=re.I)
    name = re.sub(r"'s$", "", name).strip()
    return name or label


def _drive_kind(url):
    if "/drive/folders/" in url:
        return "folder"
    return "file"


def _host(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _slug_title(slug):
    slug = urllib.parse.unquote(slug).strip("/").split("/")[-1]
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug

# --------------------------------------------------------------------------
# RSS / Atom feed parsing
# --------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_feed(text, source, editor):
    """Parse an RSS 2.0 or Atom feed document into records.

    Returns a list of records, or [] when the text is not a parseable feed.
    """
    if not text or not text.lstrip().startswith("<"):
        return []
    try:
        root = _ET.fromstring(text)
    except Exception:
        return []
    label = source.get("label", "")
    url = source.get("url", "")
    records = []
    tag = _local(root.tag)
    if tag == "feed":  # Atom
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            title = ""
            link = ""
            for child in entry:
                name = _local(child.tag)
                if name == "title":
                    title = _clean(child.text)
                elif name == "link":
                    rel = child.get("rel") or "alternate"
                    href = child.get("href") or ""
                    if rel in ("alternate", "") and href and not link:
                        link = href
                    elif href and not link:
                        link = href
            if title:
                records.append(_record(label, editor, title, link or url,
                                       "website"))
    elif tag == "rss":  # RSS 2.0
        for item in root.iter():
            if _local(item.tag) != "item":
                continue
            title = ""
            link = ""
            for child in item:
                name = _local(child.tag)
                if name == "title":
                    title = _clean(child.text)
                elif name == "link":
                    link = _clean(child.text)
            if title:
                records.append(_record(label, editor, title, link or url,
                                       "website"))
    return records


def _feed_candidates(url):
    """Ordered feed URLs to try for a site (RSS discovery, cheapest first)."""
    host = _host(url)
    base = url.rstrip("/")
    cands = []
    if "blogspot." in host:
        cands.append(base.split("?")[0].rstrip("/") + "/feeds/posts/default")
    if "wordpress.com" in host:
        cands.append(base.split("?")[0].rstrip("/") + "/feed/")
    for suffix in ("/feed/", "/rss/", "/feed.xml", "/rss.xml", "/atom.xml"):
        cand = base + suffix
        if cand not in cands:
            cands.append(cand)
    return cands


_FEED_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]*>', re.I)
_FEED_TYPE_RE = re.compile(
    r'type=["\']application/(rss\+xml|atom\+xml)["\']', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


def _discover_feed(html_text, base_url):
    """Follow <link rel="alternate" type="application/rss+xml|atom+xml">."""
    if not html_text:
        return None
    for m in _FEED_LINK_RE.finditer(html_text):
        tag = m.group(0)
        if not _FEED_TYPE_RE.search(tag):
            continue
        hm = _HREF_RE.search(tag)
        if hm:
            return urllib.parse.urljoin(base_url, hm.group(1))
    return None


def _try_feeds(ctx, state, source, editor):
    """Try RSS/Atom discovery for a website source. Returns records or None
    when no feed parsed (caller then falls back to HTML scraping)."""
    label = source.get("label", "")
    url = source.get("url", "")
    tried = set()
    for cand in _feed_candidates(url):
        if state.rate_limited or cand in tried:
            continue
        tried.add(cand)
        text = _fetch_text(ctx, state, cand, label, quiet=True)
        if text is None:
            if state.rate_limited:
                return []
            continue
        recs = _parse_feed(text, source, editor)
        if recs:
            return _dedupe(recs)
    # Autodiscovery from the homepage itself.
    if not state.rate_limited:
        home = _fetch_text(ctx, state, url, label, quiet=True)
        if home:
            feed_url = _discover_feed(home, url)
            if feed_url and feed_url not in tried:
                text = _fetch_text(ctx, state, feed_url, label, quiet=True)
                if text:
                    recs = _parse_feed(text, source, editor)
                    if recs:
                        return _dedupe(recs)
            return home  # homepage HTML, for the HTML fallback below
    return None


# --------------------------------------------------------------------------
# HTML parsing — one pass producing an ordered event list
# --------------------------------------------------------------------------

class _PageEvents(HTMLParser):
    """Collects anchors, headings and images in document order.

    events: list of tuples:
      ("a", href, text) | ("h", level, text) | ("img", src, alt)
    """

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.events = []
        self._a_href = None
        self._a_text = []
        self._h_level = None
        self._h_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            href = attrs.get("href") or ""
            if href and not href.startswith(("#", "mailto:", "javascript:")):
                self._a_href = urllib.parse.urljoin(self.base_url, href)
                self._a_text = []
        elif tag in ("h1", "h2", "h3", "h4"):
            self._h_level = tag
            self._h_text = []
        elif tag == "img":
            src = attrs.get("src") or ""
            alt = attrs.get("alt") or ""
            if src:
                self.events.append(("img", urllib.parse.urljoin(self.base_url, src), alt))

    def handle_data(self, data):
        if self._a_href is not None:
            self._a_text.append(data)
        if self._h_level is not None:
            self._h_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._a_href is not None:
            text = _clean("".join(self._a_text))
            self.events.append(("a", self._a_href, text))
            self._a_href = None
            self._a_text = []
        elif tag == self._h_level and self._h_level is not None:
            text = _clean("".join(self._h_text))
            if text:
                self.events.append(("h", self._h_level, text))
            self._h_level = None
            self._h_text = []


def _events_from_html(html_text, base_url):
    parser = _PageEvents(base_url)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return []
    return parser.events


def _img_filename_title(src):
    name = urllib.parse.unquote(src).split("/")[-1]
    name = re.sub(r"\.(jpg|jpeg|png|webp|gif|avif)$", "", name, flags=re.I)
    name = re.sub(r"^(thumb|thumbnail)[ _-]+", "", name, flags=re.I)
    if re.match(r"^(il_|[\w]{20,}$)", name) or len(name) < 3:
        return ""
    name = re.sub(r"[-_]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

# --------------------------------------------------------------------------
# Per-site HTML parsers (host-keyed). Each takes (events, source, editor)
# and returns a list of records. A parser returning [] means "no usable
# edit list found" — the caller logs the skip.
# --------------------------------------------------------------------------

_NAV_TEXT = {
    "home", "about", "contact", "search", "login", "register", "sign in",
    "sign up", "browse", "forums", "forum", "blog", "shop", "donate",
    "privacy", "terms", "faq", "rules", "members", "next", "previous",
}

def _p_apreciousblueberry(events, source, editor):
    # GitHub Pages compendium: headings link "ORIGINAL ▶ Edit Title".
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a":
            continue
        if "▶" in b:
            original, _, title = b.partition("▶")
            recs.append(_record(label, editor, title, a, "website",
                                original=_clean(original)))
    return recs


def _p_bestedit(events, source, editor):
    # Custom site: edit titles are the poster <img> alt texts.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "img":
            continue
        alt = _clean(b)
        if not alt or len(alt) < 2:
            continue
        if "best edit" in alt.lower() and "fan-edit" in alt.lower():
            continue
        recs.append(_record(label, editor, alt, url, "website"))
    return recs


def _p_bingeguy(events, source, editor):
    # One long page; each edit is an <h2> section ("*Blade Runner*").
    label, url = source["label"], source["url"]
    skip_phrases = ("what is a fan edit", "running time is not the issue",
                    "directions", "files are now on google drive",
                    "if any problems", "please download the files",
                    "scroll down to find", "table of contents",
                    "legal disclaimer")
    recs = []
    for kind, a, b in events:
        if kind != "h" or a not in ("h2", "h3"):
            continue
        title = b.strip("* ").strip()
        low = title.lower()
        if not title or len(title) < 3:
            continue
        if any(p in low for p in skip_phrases) or title.endswith("?"):
            continue
        recs.append(_record(label, editor, title, url, "website"))
    return recs


def _p_futureworld(events, source, editor):
    # Sub-pages under /fanedits/<slug>.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a":
            continue
        path = urllib.parse.urlparse(a).path
        if path.startswith("/fanedits/") and len(path) > len("/fanedits/"):
            title = _slug_title(path)
            if title:
                recs.append(_record(label, editor, title, a, "website"))
    return recs


def _p_kaipatterson(events, source, editor):
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a":
            continue
        if "patterson cut" in b.lower() or b.lower().startswith("boba fett"):
            recs.append(_record(label, editor, b, a, "website"))
    return recs


def _p_lightspeed(events, source, editor):
    # Vault pages at /vault/<slug>.html with the title as the link text.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a" or "/vault/" not in a:
            continue
        title = b.strip(" —-").strip()
        if len(title) >= 3:
            recs.append(_record(label, editor, title, a, "website"))
    return recs


def _p_tfn(events, source, editor):
    # Google Sites catalog: links to /tfn-edits/<slug> pages.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a" or "/tfn-edits/" not in a:
            continue
        title = _slug_title(a)
        if title:
            recs.append(_record(label, editor, title, a, "website"))
    return recs


def _p_hunkulous(events, source, editor):
    # Google Sites page: <h2> title, then a Drive link (file or folder).
    label, url = source["label"], source["url"]
    recs = []
    pending = None
    for kind, a, b in events:
        if kind == "h" and a in ("h2", "h3"):
            if pending:
                recs.append(_record(label, editor, pending, url, "website"))
            title = b.rstrip(":").strip()
            pending = title if title else None
        elif kind == "a" and pending and "drive.google.com" in a:
            recs.append(_record(label, editor, pending, a, "website",
                                kind=_drive_kind(a)))
            pending = None
    if pending:
        recs.append(_record(label, editor, pending, url, "website"))
    return recs


def _p_plboucher(events, source, editor):
    # Squarespace gallery: edit titles live in poster image filenames.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "img":
            continue
        if "squarespace-cdn.com" not in a:
            continue
        title = _img_filename_title(a)
        if title:
            recs.append(_record(label, editor, title, url, "website"))
    return recs


def _p_pentex(events, source, editor):
    # Squarespace: poster images; only identifiable filenames yield titles.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "img":
            continue
        low = a.lower()
        if any(s in low for s in ("logo", "youtube", "patreon", "presentation1",
                                  "pentex+purple")):
            continue
        title = _img_filename_title(a)
        if title and len(title) >= 4:
            recs.append(_record(label, editor, title, url, "website"))
    return recs


def _p_xenforo(events, source, editor):
    # fanedit.org front page: recent thread links (/threads/<slug>.<id>/).
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a" or "/threads/" not in a:
            continue
        if len(b) < 4 or b.lower() in _NAV_TEXT:
            continue
        recs.append(_record(label, editor, b, a, "website"))
    return recs


def _p_originaltrilogy(events, source, editor):
    # Forum topic list: links to /topic/<slug>/id/<n>.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a" or "/topic/" not in a:
            continue
        if len(b) < 4 or b.lower() in _NAV_TEXT:
            continue
        recs.append(_record(label, editor, b, a, "website"))
    return recs


def _p_fanrestore(events, source, editor):
    # MyBB-style forum: thread links (showthread.php?tid=...).
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a" or "showthread" not in a:
            continue
        if len(b) < 4 or b.lower() in _NAV_TEXT:
            continue
        recs.append(_record(label, editor, b, a, "website"))
    return recs


def _p_generic_thread(events, source, editor):
    # Fallback for other forum hubs (e.g. MRDb): any anchor with "thread"
    # in the URL and a plausible title.
    label, url = source["label"], source["url"]
    recs = []
    for kind, a, b in events:
        if kind != "a" or "thread" not in a.lower():
            continue
        if len(b) < 4 or b.lower() in _NAV_TEXT:
            continue
        recs.append(_record(label, editor, b, a, "website"))
    return recs


# Host substring -> parser. Checked in order; first match wins.
_SITE_PARSERS = [
    ("apreciousblueberry.github.io", _p_apreciousblueberry),
    ("bestedit.net", _p_bestedit),
    ("bingeguy.com", _p_bingeguy),
    ("futureworldmultimedia.com", _p_futureworld),
    ("kaipattersonfilms.com", _p_kaipatterson),
    ("light-speed.studio", _p_lightspeed),
    ("thefaneditnetwork", _p_tfn),
    ("hunkulous-edits", _p_hunkulous),
    ("plboucherdesigns.com", _p_plboucher),
    ("editsbypentex.com", _p_pentex),
    ("fanedit.org", _p_xenforo),
    ("originaltrilogy.com", _p_originaltrilogy),
    ("fanrestore.com", _p_fanrestore),
    ("moviesremastered.com", _p_generic_thread),
]

# Sites probed 2026-11-05 where no usable edit list exists. The adapter
# still returns [] + a log line for these instead of wasting fetches.
# (Blogspot / WordPress sites are NOT here — the core RSS adapter owns them;
#  this module only falls back to them when a source is dispatched here with
#  adapter "website".)
_SKIP_SITES = {
    "linktr.ee": "link aggregator, no edit list",
    "x.com": "social profile, not an edit listing",
    "twitter.com": "social profile, not an edit listing",
    "youtube.com": "video platform, out of scope for the website adapter",
    "youtu.be": "video platform, out of scope for the website adapter",
    "rutracker.org": "forum, login/translate gated",
    "archive.ph": "archive snapshot not fetchable from probe environment",
    "forums.thestarwarstrilogy.com": "not fetchable from probe environment",
    "maple-films.com": "production-company site, not a fan-edit listing",
    "lordalvarezedits.weebly.com": "no extractable content on projects page",
    "firemerkstudios.com": "vault under maintenance, no edit titles in page",
    "faneditcentral.wixsite.com": "Wix app content not present in static HTML",
    "fanworx.org": "community site in development, no edit database yet",
    "sites.google.com/view/faneditfanclub": "own club site; collection covered by core Drive adapter",
    "sites.google.com/student.iolaisd.net": "category index only; edits live on subpages not enumerated on homepage",
    "defuse.ca": "password-gated pastebin mirror, not covered by core Pastebin adapter",
}


def _site_parser_for(host):
    for needle, fn in _SITE_PARSERS:
        if needle in host:
            return fn
    return None


def _skip_reason_for(url):
    low = url.lower()
    for needle, reason in _SKIP_SITES.items():
        if needle in low:
            return reason
    return None


def adapt_website(source, ctx):
    """Website adapter: RSS/Atom feed first, then a per-site HTML parser.

    Never raises. Any fetch / parse failure (including HTTP 429) returns
    [] and logs the skip.
    """
    label = source.get("label", "")
    url = source.get("url", "")
    if not url:
        _log(ctx, "SKIP %s: no URL" % label)
        return []
    reason = _skip_reason_for(url)
    if reason:
        _log(ctx, "SKIP %s: %s" % (label, reason))
        return []
    editor = _editor_from_source(source)
    host = _host(url)
    state = _State()
    try:
        # Google Sites pages have no feeds; go straight to HTML parsing.
        if "sites.google.com" not in host:
            feed_result = _try_feeds(ctx, state, source, editor)
            if state.rate_limited:
                _log(ctx, "SKIP %s: rate-limited (HTTP 429) — retry next run"
                     % label)
                return []
            if isinstance(feed_result, list) and feed_result:
                return feed_result
            homepage = feed_result if isinstance(feed_result, str) else None
        else:
            homepage = None
        if state.rate_limited:
            _log(ctx, "SKIP %s: rate-limited (HTTP 429) — retry next run"
                 % label)
            return []
        if homepage is None:
            homepage = _fetch_text(ctx, state, url, label)
        if state.rate_limited:
            _log(ctx, "SKIP %s: rate-limited (HTTP 429) — retry next run"
                 % label)
            return []
        if not homepage:
            _log(ctx, "SKIP %s: could not fetch %s" % (label, url))
            return []
        parser = _site_parser_for(host)
        if parser is None:
            _log(ctx, "SKIP %s: no feed and no HTML parser for %s"
                 % (label, host))
            return []
        events = _events_from_html(homepage, url)
        recs = parser(events, source, editor)
        recs = _dedupe(recs)
        if not recs:
            _log(ctx, "SKIP %s: no edit list found on %s" % (label, url))
            return []
        return recs
    except Exception as exc:  # noqa: BLE001 - adapter must never raise
        if _is_rate_limit(exc):
            _log(ctx, "SKIP %s: rate-limited — retry next run" % label)
        else:
            _log(ctx, "SKIP %s: %s" % (label, _short_err(exc)))
        return []

# --------------------------------------------------------------------------
# Pure-Python AES-128 (needed: Mega encrypts node names/keys with AES).
# S-box is computed at import from the GF(2^8) definition, so there are no
# hand-transcribed tables to get wrong. Validated against the FIPS-197
# test vector in the __main__ self-test below.
# --------------------------------------------------------------------------

def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF


def _gf_mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _make_sbox():
    sbox = []
    for i in range(256):
        if i == 0:
            inv = 0
        else:
            inv = 1
            x = i
            e = 254
            # inv = i ** 254 in GF(2^8)
            result = 1
            base = i
            while e:
                if e & 1:
                    result = _gf_mul(result, base)
                base = _gf_mul(base, base)
                e >>= 1
            inv = result
        s = inv
        for _ in range(4):
            s = ((s << 1) | (s >> 7)) & 0xFF
            inv ^= s
        sbox.append((inv ^ 0x63) & 0xFF)
    return sbox


_SBOX = _make_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key):
    # key: 16 bytes -> 11 round keys, each 16 bytes
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [bytes(sum(w[4 * r:4 * r + 4], [])) for r in range(11)]


def _sub_bytes(state, box):
    return [box[b] for b in state]


def _shift_rows(state):
    # state is column-major: index = col*4 + row
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = state[((c + r) % 4) * 4 + r]
    return out


def _inv_shift_rows(state):
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = state[((c - r) % 4) * 4 + r]
    return out


def _mix_columns(state):
    out = [0] * 16
    for c in range(4):
        s = state[c * 4:c * 4 + 4]
        out[c * 4 + 0] = _gf_mul(s[0], 2) ^ _gf_mul(s[1], 3) ^ s[2] ^ s[3]
        out[c * 4 + 1] = s[0] ^ _gf_mul(s[1], 2) ^ _gf_mul(s[2], 3) ^ s[3]
        out[c * 4 + 2] = s[0] ^ s[1] ^ _gf_mul(s[2], 2) ^ _gf_mul(s[3], 3)
        out[c * 4 + 3] = _gf_mul(s[0], 3) ^ s[1] ^ s[2] ^ _gf_mul(s[3], 2)
    return out


def _inv_mix_columns(state):
    out = [0] * 16
    for c in range(4):
        s = state[c * 4:c * 4 + 4]
        out[c * 4 + 0] = (_gf_mul(s[0], 14) ^ _gf_mul(s[1], 11)
                          ^ _gf_mul(s[2], 13) ^ _gf_mul(s[3], 9))
        out[c * 4 + 1] = (_gf_mul(s[0], 9) ^ _gf_mul(s[1], 14)
                          ^ _gf_mul(s[2], 11) ^ _gf_mul(s[3], 13))
        out[c * 4 + 2] = (_gf_mul(s[0], 13) ^ _gf_mul(s[1], 9)
                          ^ _gf_mul(s[2], 14) ^ _gf_mul(s[3], 11))
        out[c * 4 + 3] = (_gf_mul(s[0], 11) ^ _gf_mul(s[1], 13)
                          ^ _gf_mul(s[2], 9) ^ _gf_mul(s[3], 14))
    return out


def _add_round_key(state, rk):
    return [b ^ k for b, k in zip(state, rk)]


def _aes_encrypt_block(key, block):
    rk = _expand_key(key)
    state = _add_round_key(list(block), rk[0])
    for rnd in range(1, 10):
        state = _sub_bytes(state, _SBOX)
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, rk[rnd])
    state = _sub_bytes(state, _SBOX)
    state = _shift_rows(state)
    state = _add_round_key(state, rk[10])
    return bytes(state)


def _aes_decrypt_block(key, block):
    rk = _expand_key(key)
    state = _add_round_key(list(block), rk[10])
    for rnd in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = _sub_bytes(state, _INV_SBOX)
        state = _add_round_key(state, rk[rnd])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = _sub_bytes(state, _INV_SBOX)
    state = _add_round_key(state, rk[0])
    return bytes(state)


def _aes_cbc_decrypt(key, data):
    out = bytearray()
    prev = b"\x00" * 16
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        if len(blk) < 16:
            break
        dec = _aes_decrypt_block(key, blk)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = blk
    return bytes(out)


def _aes_cbc_encrypt(key, data):
    out = bytearray()
    prev = b"\x00" * 16
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        xored = bytes(x ^ y for x, y in zip(blk, prev))
        enc = _aes_encrypt_block(key, xored)
        out += enc
        prev = enc
    return bytes(out)


def _b64url_decode(s):
    s = (s or "").strip()
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _b64url_encode(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------
# Mega (anonymous public-folder API)
# --------------------------------------------------------------------------

_MEGA_API = "https://g.api.mega.co.nz/cs?id=0"
_MEGA_ERR = {-1: "internal error", -2: "args error", -3: "again (retry)",
             -4: "rate limit", -5: "failed", -6: "too many",
             -9: "not found", -11: "access denied", -13: "expired",
             -14: "wrong key", -15: "blocked", -16: "over quota",
             -17: "temp unavailable", -18: "too many connections"}


def _mega_decrypt_attr(key16, b64attr):
    """Decrypt a node's 'a' attribute. Returns the attribute dict or None."""
    try:
        raw = _b64url_decode(b64attr)
        if len(raw) == 0 or len(raw) % 16:
            return None
        pt = _aes_cbc_decrypt(key16, raw).rstrip(b"\x00")
        if not pt.startswith(b"MEGA"):
            return None
        return json.loads(pt[4:].decode("utf-8"))
    except Exception:
        return None


def _mega_unwrap_key(parent_key16, k_field):
    """Decrypt a node's 'k' (key) field with its parent's key (AES-ECB)."""
    try:
        enc = (k_field or "").split(":", 1)[-1]
        raw = _b64url_decode(enc)
        if len(raw) not in (16, 32):
            return None
        out = b""
        for i in range(0, len(raw), 16):
            out += _aes_decrypt_block(parent_key16, raw[i:i + 16])
        return out
    except Exception:
        return None


def _mega_parse_folder_link(url):
    """Return (handle, key_bytes, sub_handle) or None."""
    try:
        parsed = urllib.parse.urlparse(url)
        frag = parsed.fragment
        m = re.search(r"folder/([A-Za-z0-9_-]+)", parsed.path)
        if not m:
            m = re.search(r"folder/([A-Za-z0-9_-]+)", url)
        if not m:
            return None
        handle = m.group(1)
        parts = frag.split("/") if frag else []
        if not parts:
            return None
        key = _b64url_decode(parts[0])
        if len(key) != 16:
            return None
        sub = None
        if len(parts) >= 3 and parts[1] == "folder":
            sub = parts[2]
        return handle, key, sub
    except Exception:
        return None


def adapt_mega(source, ctx):
    """Anonymous Mega public-folder listing. Never raises; on any failure
    (including HTTP 429) returns [] and logs the skip."""
    label = source.get("label", "")
    url = source.get("url", "")
    editor = _editor_from_source(source)
    parsed = _mega_parse_folder_link(url)
    if not parsed:
        _log(ctx, "SKIP %s: not a parseable Mega folder link: %s" % (label, url))
        return []
    handle, key, sub_handle = parsed
    state = _State()
    try:
        body = json.dumps([{"a": "f", "c": 1, "r": 1, "n": handle}]).encode("utf-8")
        raw = _fetch_bytes_direct(
            _MEGA_API, label, ctx, state, data=body,
            headers={"Content-Type": "application/json"})
        if raw is None:
            if not state.rate_limited:
                _log(ctx, "SKIP %s: Mega API request failed" % label)
            return []
        try:
            resp = json.loads(raw.decode("utf-8"))
        except Exception:
            _log(ctx, "SKIP %s: Mega API returned unparseable response" % label)
            return []
        if isinstance(resp, list):
            if resp and isinstance(resp[0], int) and resp[0] < 0:
                err = _MEGA_ERR.get(resp[0], "error %d" % resp[0])
                if resp[0] == -4:
                    _log(ctx, "SKIP %s: rate-limited by Mega (%s) — retry next run"
                         % (label, err))
                else:
                    _log(ctx, "SKIP %s: Mega API error: %s" % (label, err))
                return []
            resp = resp[0] if resp else {}
        if not isinstance(resp, dict) or "f" not in resp:
            _log(ctx, "SKIP %s: Mega API returned no file list" % label)
            return []
        nodes = resp["f"]
        by_handle = {}
        for n in nodes:
            if isinstance(n, dict) and n.get("h"):
                by_handle[n["h"]] = n
        root_handle = sub_handle or handle
        root = by_handle.get(root_handle)
        if root is None:
            _log(ctx, "SKIP %s: Mega folder root not in listing (private/removed?)"
                 % label)
            return []
        records = []
        # BFS from the root; each node's key unwraps with its parent's key.
        queue = [(root_handle, key)]
        seen = set()
        while queue:
            parent_h, parent_key = queue.pop(0)
            for n in nodes:
                if not isinstance(n, dict) or n.get("p") != parent_h:
                    continue
                h = n.get("h")
                if not h or h in seen:
                    continue
                seen.add(h)
                ntype = n.get("t")
                if ntype == 1:  # folder
                    child_key = _mega_unwrap_key(parent_key, n.get("k", ""))
                    if child_key is None:
                        continue
                    attrs = _mega_decrypt_attr(child_key[:16], n.get("a", ""))
                    name = (attrs or {}).get("n") or ""
                    if name:
                        records.append(_record(label, editor, name, url,
                                               "cloud", kind="folder"))
                    queue.append((h, child_key[:16]))
                elif ntype == 0:  # file
                    child_key = _mega_unwrap_key(parent_key, n.get("k", ""))
                    if child_key is None:
                        continue
                    attrs = _mega_decrypt_attr(child_key[:16], n.get("a", ""))
                    name = (attrs or {}).get("n") or ""
                    if name:
                        records.append(_record(label, editor, name, url,
                                               "cloud", kind="file"))
        if not records:
            _log(ctx, "SKIP %s: Mega folder listing empty or undecryptable"
                 % label)
            return []
        return _dedupe(records)
    except Exception as exc:  # noqa: BLE001 - adapter must never raise
        if _is_rate_limit(exc):
            _log(ctx, "SKIP %s: rate-limited — retry next run" % label)
        else:
            _log(ctx, "SKIP %s: %s" % (label, _short_err(exc)))
        return []

# --------------------------------------------------------------------------
# GoFile (guest-token contents API)
# --------------------------------------------------------------------------

_GOFILE_API = "https://api.gofile.io"


def _gofile_children(data, source, editor, records, depth=0):
    children = data.get("children") or {}
    for child in children.values():
        if not isinstance(child, dict):
            continue
        name = child.get("name") or ""
        ctype = child.get("type")
        link = child.get("link") or source["url"]
        if ctype == "folder":
            if name:
                records.append(_record(source["label"], editor, name, link,
                                       "cloud", kind="folder"))
            if depth < 2:
                _gofile_children(child, source, editor, records, depth + 1)
        elif ctype == "file":
            if name:
                records.append(_record(source["label"], editor, name, link,
                                       "cloud", kind="file"))


def adapt_gofile(source, ctx):
    """GoFile public link listing via a free anonymous guest token.

    Never raises. As of 2026-11-05 the contents endpoint answers
    "error-notPremium" to guest tokens, so public folders are currently
    not listable anonymously — that is logged as a skip, not an error.
    """
    label = source.get("label", "")
    url = source.get("url", "")
    editor = _editor_from_source(source)
    m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", url)
    if not m:
        _log(ctx, "SKIP %s: not a parseable GoFile link: %s" % (label, url))
        return []
    content_id = m.group(1)
    state = _State()
    try:
        raw = _fetch_bytes_direct(
            _GOFILE_API + "/accounts", label, ctx, state, data=b"{}",
            headers={"Content-Type": "application/json"})
        if raw is None:
            if not state.rate_limited:
                _log(ctx, "SKIP %s: GoFile guest-token request failed" % label)
            return []
        try:
            acct = json.loads(raw.decode("utf-8"))
        except Exception:
            _log(ctx, "SKIP %s: GoFile guest-token response unparseable" % label)
            return []
        token = ((acct.get("data") or {}).get("token")) if isinstance(acct, dict) else None
        if not token:
            _log(ctx, "SKIP %s: GoFile guest-token request returned no token"
                 % label)
            return []
        raw = _fetch_bytes_direct(
            "%s/contents/%s?wt=4&cache=true" % (_GOFILE_API, content_id),
            label, ctx, state,
            headers={"Authorization": "Bearer " + token})
        if raw is None:
            if not state.rate_limited:
                _log(ctx, "SKIP %s: GoFile contents request failed" % label)
            return []
        try:
            resp = json.loads(raw.decode("utf-8"))
        except Exception:
            _log(ctx, "SKIP %s: GoFile contents response unparseable" % label)
            return []
        status = resp.get("status") if isinstance(resp, dict) else None
        if status != "ok":
            if status == "error-notPremium":
                _log(ctx, "SKIP %s: GoFile contents API requires a premium "
                     "account; public folder not listable anonymously" % label)
            elif status == "error-notFound":
                _log(ctx, "SKIP %s: GoFile content not found/removed" % label)
            else:
                _log(ctx, "SKIP %s: GoFile API status: %s" % (label, status))
            return []
        data = resp.get("data") or {}
        records = []
        if data.get("type") == "file":
            if data.get("name"):
                records.append(_record(label, editor, data["name"],
                                       data.get("link") or url, "cloud",
                                       kind="file"))
        else:
            if data.get("name"):
                records.append(_record(label, editor, data["name"], url,
                                       "cloud", kind="folder"))
            _gofile_children(data, source, editor, records)
        if not records:
            _log(ctx, "SKIP %s: GoFile folder empty" % label)
            return []
        return _dedupe(records)
    except Exception as exc:  # noqa: BLE001 - adapter must never raise
        if _is_rate_limit(exc):
            _log(ctx, "SKIP %s: rate-limited — retry next run" % label)
        else:
            _log(ctx, "SKIP %s: %s" % (label, _short_err(exc)))
        return []


# --------------------------------------------------------------------------
# Yandex Disk (anonymous public-resources API)
# --------------------------------------------------------------------------

_YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"


def _yandex_walk(public_key, path, source, editor, ctx, state, records,
                 depth=0):
    params = {"public_key": public_key, "limit": 100, "offset": 0}
    if path:
        params["path"] = path
    while True:
        q = urllib.parse.urlencode(params)
        text = _fetch_text(ctx, state, _YANDEX_API + "?" + q,
                           source["label"], quiet=True)
        if text is None:
            return
        try:
            resp = json.loads(text)
        except Exception:
            return
        if "message" in resp and "error" in str(resp.get("error", "")).lower():
            return
        rtype = resp.get("type")
        if rtype == "file":
            if resp.get("name"):
                records.append(_record(source["label"], editor, resp["name"],
                                       resp.get("public_url") or source["url"],
                                       "cloud", kind="file"))
            return
        emb = resp.get("_embedded") or {}
        items = emb.get("items") or []
        total = emb.get("total") or len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or ""
            if not name:
                continue
            if item.get("type") == "dir":
                records.append(_record(source["label"], editor, name,
                                       item.get("public_url") or source["url"],
                                       "cloud", kind="folder"))
                if depth < 2 and item.get("path"):
                    _yandex_walk(public_key, item["path"], source, editor,
                                 ctx, state, records, depth + 1)
                    if state.rate_limited:
                        return
            else:
                records.append(_record(source["label"], editor, name,
                                       item.get("public_url") or source["url"],
                                       "cloud", kind="file"))
        params["offset"] += len(items)
        if not items or params["offset"] >= total:
            return


def adapt_yandex(source, ctx):
    """Yandex Disk public link listing (anonymous API, no token).
    Never raises; any failure (including HTTP 429) returns [] + log."""
    label = source.get("label", "")
    url = source.get("url", "")
    editor = _editor_from_source(source)
    if "yandex" not in url:
        _log(ctx, "SKIP %s: not a Yandex link: %s" % (label, url))
        return []
    state = _State()
    try:
        records = []
        _yandex_walk(url, None, source, editor, ctx, state, records)
        if state.rate_limited:
            _log(ctx, "SKIP %s: rate-limited (HTTP 429) — retry next run"
                 % label)
            return []
        if not records:
            _log(ctx, "SKIP %s: Yandex public resource empty or unreachable"
                 % label)
            return []
        return _dedupe(records)
    except Exception as exc:  # noqa: BLE001 - adapter must never raise
        if _is_rate_limit(exc):
            _log(ctx, "SKIP %s: rate-limited — retry next run" % label)
        else:
            _log(ctx, "SKIP %s: %s" % (label, _short_err(exc)))
        return []


# --------------------------------------------------------------------------
# Dispatcher / public contract
# --------------------------------------------------------------------------

EXTRA_ADAPTERS = {
    "website": adapt_website,
    "mega": adapt_mega,
    "gofile": adapt_gofile,
    "yandex": adapt_yandex,
}


def adapt(source, ctx):
    """Dispatch one sources.json entry to its adapter. Never raises."""
    label = (source or {}).get("label", "")
    name = (source or {}).get("adapter", "")
    fn = EXTRA_ADAPTERS.get(name)
    if fn is None:
        _log(ctx, "SKIP %s: no extra adapter for %r" % (label, name))
        return []
    try:
        return fn(source, ctx)
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        if _is_rate_limit(exc):
            _log(ctx, "SKIP %s: rate-limited — retry next run" % label)
        else:
            _log(ctx, "SKIP %s: %s" % (label, _short_err(exc)))
        return []


# --------------------------------------------------------------------------
# Self-test / demo harness (run: python3 adapters_extra.py [--selftest])
# --------------------------------------------------------------------------

def _urllib_ctx(logs):
    def fetch_text(url, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def fetch_bytes(url, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return {"fetch_text": fetch_text, "fetch_bytes": fetch_bytes,
            "log": lambda m: logs.append(m)}


def _selftest():
    # 1. AES-128 against the FIPS-197 test vector.
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = _aes_encrypt_block(key, pt)
    assert ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex()
    assert _aes_decrypt_block(key, ct) == pt
    print("AES-128 FIPS-197 vector: OK")

    # 2. Mega round-trip on a synthetic encrypted folder listing.
    folder_key = bytes(range(16))
    file_key = bytes(range(16, 48))
    # child file node key, ECB-encrypted with the folder key
    enc_file_key = (_aes_encrypt_block(folder_key, file_key[:16])
                    + _aes_encrypt_block(folder_key, file_key[16:]))
    attr = b"MEGA" + json.dumps({"n": "Test Edit - Fan Edit.mp4"}).encode()
    attr += b"\x00" * (-len(attr) % 16)
    enc_attr = _aes_cbc_encrypt(file_key[:16], attr)
    node_key = _mega_unwrap_key(folder_key, "XXXX:" + _b64url_encode(enc_file_key))
    assert node_key == file_key, node_key.hex()
    attrs = _mega_decrypt_attr(file_key[:16], _b64url_encode(enc_attr))
    assert attrs and attrs["n"] == "Test Edit - Fan Edit.mp4", attrs
    print("Mega encrypt/decrypt round-trip: OK")

    # 3. Rate-limit handling: a ctx that always raises HTTP 429 must
    #    produce [] + a rate-limited log line, never an exception.
    import urllib.error as _ue
    logs = []

    def raiser(url, timeout=30):
        raise _ue.HTTPError(url, 429, "Too Many Requests", {}, None)

    ctx = {"fetch_text": raiser, "fetch_bytes": raiser,
           "log": lambda m: logs.append(m)}
    out = adapt({"label": "Test Site", "url": "https://example.com/",
                 "adapter": "website"}, ctx)
    assert out == [], out
    assert any("rate-limited" in m for m in logs), logs
    out = adapt({"label": "Test Mega",
                 "url": "https://mega.nz/folder/AAAAAAAA#AAAAAAAAAAAAAAAAAAAAAA",
                 "adapter": "mega"}, ctx)
    assert out == [], out
    print("429 -> skip + log, never raises: OK")

    # 4. Feed parsing on a canned Atom document.
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><title>Example Cut</title>'
            '<link rel="alternate" href="https://example.com/post1"/>'
            '</entry></feed>')
    recs = _parse_feed(atom, {"label": "T", "url": "https://example.com/"},
                       "Ed")
    assert len(recs) == 1 and recs[0]["title"] == "Example Cut", recs
    print("Atom feed parse: OK")
    print("SELFTEST PASSED")


_DEMO_SOURCES = [
    {"label": "AWW Fan Edits", "url": "https://americanwadewilson.blogspot.com/",
     "adapter": "website"},
    {"label": "APreciousBlueberry's FAN-EDIT Compendium",
     "url": "https://apreciousblueberry.github.io/", "adapter": "website"},
    {"label": "Best Edit Fan Edits", "url": "https://bestedit.net/",
     "adapter": "website"},
    {"label": "BingeGuy Fan Edits", "url": "https://bingeguy.com/starwars/",
     "adapter": "website"},
    {"label": "Future World Multimedia",
     "url": "https://www.futureworldmultimedia.com/fanedits",
     "adapter": "website"},
    {"label": "Kai Patterson Fan Edits",
     "url": "https://www.kaipattersonfilms.com/fanedits", "adapter": "website"},
    {"label": "Light Speed Studios", "url": "https://www.light-speed.studio/",
     "adapter": "website"},
    {"label": "The Fan Edit Network",
     "url": "https://sites.google.com/view/thefaneditnetwork/tfn-catalog",
     "adapter": "website"},
    {"label": "Hunkulous' Edits",
     "url": "https://sites.google.com/view/hunkulous-edits",
     "adapter": "website"},
    {"label": "NOTFLIX Fan Edits", "url": "https://notflixfanedits.com/",
     "adapter": "website"},
    {"label": "Martokc Extended Edition Fan Edits",
     "url": "https://disk.yandex.ru/d/QdXTdvYlWl-QtQ", "adapter": "yandex"},
    {"label": "Deckmx Fan Edits", "url": "https://gofile.io/d/ekYUlP",
     "adapter": "gofile"},
    {"label": "Krypton7887 Superman Fan Edits",
     "url": "https://mega.nz/folder/VLYwnICK#IK9Z8B8jPPdwhfUIIKb2CA",
     "adapter": "mega"},
]


def _demo():
    logs = []
    ctx = _urllib_ctx(logs)
    for src in _DEMO_SOURCES:
        try:
            recs = adapt(src, ctx)
        except Exception as exc:  # noqa: BLE001 - harness must not die
            print("%-42s RAISED %s" % (src["label"], exc))
            continue
        print("%-42s %3d records" % (src["label"], len(recs)))
        for r in recs[:3]:
            print("    - %s [%s] %s" % (r["title"], r["kind"], r["url"][:80]))
    print("--- logs ---")
    for m in logs:
        print("  " + m)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _demo()
