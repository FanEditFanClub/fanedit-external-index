"""Tests for doc_sync.py: Master Links doc -> sources.json regeneration.

Uses fake doc HTML (no network) except the module's own fetch, which
is stubbed with a fake opener in the end-to-end test.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from doc_sync import (  # noqa: E402
    entry_key,
    infer_source,
    parse_doc_entries,
    sync_manifest,
    sync_sources,
    unwrap_url,
)

FAKE_DOC_HTML = """
<h2>Registries:</h2>
<ul>
<li><a href="https://www.google.com/url?q=http://fanedit.org&amp;sa=D">FANEDIT.ORG</a></li>
<li><a href="https://www.google.com/url?q=https://fanedit.org/fanedit-search/&amp;sa=D">Fan Edit Search</a></li>
</ul>
<h2>Fan Edit Collections:</h2>
<h3>Google Drive &amp; Cloud Storage Links</h3>
<ul>
<li><a href="https://www.google.com/url?q=https://drive.google.com/drive/folders/AAA111AAA111AAA111AAA111AAA1&amp;sa=D">AcyliaEditz Fan Edits</a></li>
<li><a href="https://www.google.com/url?q=https://drive.google.com/drive/folders/AAA111AAA111AAA111AAA111AAA1&amp;sa=D">chasedixon22's Fan Edits</a></li>
<li><a href="https://www.google.com/url?q=https://pastebin.com/3j6pBdn1&amp;sa=D">RetroStranger95's Fan Edits</a> (<a href="https://www.google.com/url?q=https://pastebin.com/3j6pBdn1&amp;sa=D">Password: </a><a href="https://www.google.com/url?q=https://pastebin.com/3j6pBdn1&amp;sa=D">J120qPS2wqe</a>)</li>
<li><a href="https://www.google.com/url?q=https://docs.google.com/spreadsheets/d/SHEET111111111111111111111111/edit?gid=0%23gid=0&amp;sa=D">The Family Edit Database - Movies</a></li>
<li><a href="https://www.google.com/url?q=https://docs.google.com/document/u/0/d/DOC1111111111111111111111111/mobilebasic&amp;sa=D">Cuts of Numeral Joker (AKA Mecha Salesman)</a></li>
<li><a href="https://www.google.com/url?q=https://www.dropbox.com/scl/fo/xyz&amp;sa=D">Derekwkim Fan Edits</a></li>
<li><a href="https://www.google.com/url?q=https://docs.google.com/forms/d/e/FORM1/viewform&amp;sa=D">DigModiFicaTion Fan Edits</a></li>
</ul>
<h3>Websites, Blogs, etc:</h3>
<ul>
<li><a href="https://www.google.com/url?q=https://americanwadewilson.blogspot.com/&amp;sa=D">AWW Fan Edits</a></li>
<li><a href="https://www.google.com/url?q=https://bestedit.net/&amp;sa=D">Best Edit Fan Edits</a></li>
<li><a href="https://www.google.com/url?q=https://drive.google.com/drive/folders/1kGaeK1unqWD1VOl3-DGWLAxISBhDR3cg?usp=drive_link&amp;sa=D">Fan Edit Fan Club Collection</a></li>
</ul>
<h2>Search Engines:</h2>
<ul>
<li><a href="https://www.google.com/url?q=https://archive.org/search?query=Fan+Edit&amp;sa=D">Archive.org - Fan Edit Search</a></li>
<li><a href="https://www.google.com/url?q=https://fanedit.org/fanedit-search/&amp;sa=D">FANEDIT.ORG - Fan Edit Search</a></li>
<li><a href="https://www.google.com/url?q=https://x.ai/bot/DR18SV4AEeeNRcKT1U-0P&amp;sa=D">Grok Bot - Fan Edit Optimus</a></li>
</ul>
<h2>Discord Servers:</h2>
<ul>
<li><a href="https://www.google.com/url?q=https://discord.gg/d8A9xHTey7&amp;sa=D">Fan Edit Fan Club</a></li>
</ul>
<h2>Facebook Groups:</h2>
<ul>
<li><a href="https://www.google.com/url?q=https://www.facebook.com/groups/faneditfanclub&amp;sa=D">Fan Edit Fan Club</a></li>
</ul>
<h2>Fan Edit Fan Club Channels:</h2>
<ul>
<li><a href="https://www.google.com/url?q=https://www.youtube.com/@FanEditFanClub&amp;sa=D">YouTube</a></li>
<li><a href="https://www.google.com/url?q=https://www.threads.net/@faneditfanclub&amp;sa=D">Threads</a></li>
</ul>
"""


def test_unwrap_google_redirect():
    wrapped = ("https://www.google.com/url?q=https://pastebin.com/3j6pBdn1"
               "&sa=D&source=editors&ust=1&usg=abc")
    assert unwrap_url(wrapped) == "https://pastebin.com/3j6pBdn1"
    assert unwrap_url("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_parse_doc_entries_sections_and_password():
    entries = parse_doc_entries(FAKE_DOC_HTML)
    by_label = {e["label"]: e for e in entries}
    assert by_label["FANEDIT.ORG"]["section"] == "Registries:"
    assert by_label["AcyliaEditz Fan Edits"]["section"] == "Fan Edit Collections:"
    assert by_label["AcyliaEditz Fan Edits"]["subsection"] == "Google Drive & Cloud Storage Links"
    assert by_label["AWW Fan Edits"]["subsection"] == "Websites, Blogs, etc:"
    assert by_label["Archive.org - Fan Edit Search"]["section"] == "Search Engines:"
    paste = by_label["RetroStranger95's Fan Edits"]
    assert paste["password"] == "J120qPS2wqe"
    assert paste["url"] == "https://pastebin.com/3j6pBdn1"
    assert by_label["YouTube"]["section"] == "Fan Edit Fan Club Channels:"


def test_infer_drive_folder_and_duplicate_key():
    drive = infer_source({"label": "AcyliaEditz Fan Edits",
                          "url": "https://drive.google.com/drive/folders/AAA111AAA111AAA111AAA111AAA1",
                          "section": "Fan Edit Collections:", "password": ""})
    assert drive == {"label": "AcyliaEditz Fan Edits",
                     "url": "https://drive.google.com/drive/folders/AAA111AAA111AAA111AAA111AAA1",
                     "adapter": "drive_folder", "type": "source"}
    assert entry_key(drive) == "AAA111AAA111AAA111AAA111AAA1"


def test_infer_doc_with_u_segment():
    doc = infer_source({"label": "Cuts of Numeral Joker (AKA Mecha Salesman)",
                        "url": "https://docs.google.com/document/u/0/d/DOC1111111111111111111111111/mobilebasic",
                        "section": "Fan Edit Collections:", "password": ""})
    assert doc["adapter"] == "doc"
    assert doc["type"] == "source"


def test_infer_sheet_gid():
    sheet = infer_source({"label": "The Family Edit Database - Movies",
                          "url": "https://docs.google.com/spreadsheets/d/SHEET111111111111111111111111/edit?gid=0#gid=0",
                          "section": "Fan Edit Collections:", "password": ""})
    assert sheet["adapter"] == "sheet"
    assert sheet["gid"] == "0"
    assert sheet["note"] == "Sheet tab selected by gid in URL."


def test_infer_blogspot_becomes_rss():
    rss = infer_source({"label": "AWW Fan Edits",
                        "url": "https://americanwadewilson.blogspot.com/",
                        "section": "Fan Edit Collections:", "password": ""})
    assert rss["adapter"] == "rss"
    assert rss["url"] == "https://americanwadewilson.blogspot.com/feeds/posts/default"
    assert rss["site_url"] == "https://americanwadewilson.blogspot.com/"


def test_infer_club_own_is_out_of_scope():
    club = infer_source({"label": "Fan Edit Fan Club Collection",
                         "url": "https://drive.google.com/drive/folders/1kGaeK1unqWD1VOl3-DGWLAxISBhDR3cg?usp=drive_link",
                         "section": "Fan Edit Collections:", "password": ""})
    assert club["type"] == "out_of_scope"
    yt = infer_source({"label": "YouTube",
                       "url": "https://www.youtube.com/@FanEditFanClub",
                       "section": "Fan Edit Fan Club Channels:", "password": ""})
    assert yt["type"] == "out_of_scope"


def test_infer_search_engines_and_bot_note():
    tpl = infer_source({"label": "Archive.org - Fan Edit Search",
                        "url": "https://archive.org/search?query=Fan+Edit",
                        "section": "Search Engines:", "password": ""})
    assert tpl["type"] == "search_template"
    assert tpl["note"] == "Query-URL template for later fallback use; not indexed."
    bot = infer_source({"label": "Grok Bot - Fan Edit Optimus",
                        "url": "https://x.ai/bot/DR18SV4AEeeNRcKT1U-0P",
                        "section": "Search Engines:", "password": ""})
    assert bot["note"] == "Bot link listed with the search engines; not a query template."


def test_infer_reddit_discord_facebook_out_of_scope():
    assert infer_source({"label": "r/FanEditCentral",
                         "url": "https://www.reddit.com/r/FanEditCentral/",
                         "section": "Fan Edit Collections:", "password": ""})["type"] == "out_of_scope"
    assert infer_source({"label": "Fan Edit Fan Club",
                         "url": "https://discord.gg/d8A9xHTey7",
                         "section": "Discord Servers:", "password": ""})["type"] == "out_of_scope"
    assert infer_source({"label": "Fan Edit Fan Club",
                         "url": "https://www.facebook.com/groups/faneditfanclub",
                         "section": "Facebook Groups:", "password": ""})["type"] == "out_of_scope"


def test_infer_dropbox_and_form_skipped():
    assert infer_source({"label": "Derekwkim Fan Edits",
                         "url": "https://www.dropbox.com/scl/fo/xyz",
                         "section": "Fan Edit Collections:", "password": ""})["type"] == "skipped"
    assert infer_source({"label": "DigModiFicaTion Fan Edits",
                         "url": "https://docs.google.com/forms/d/e/FORM1/viewform",
                         "section": "Fan Edit Collections:", "password": ""})["type"] == "skipped"


def test_sync_dedups_same_folder_and_keeps_cross_section():
    entries = parse_doc_entries(FAKE_DOC_HTML)
    new, report = sync_sources([], entries)
    by_label = {e["label"]: e for e in new}
    assert by_label["chasedixon22's Fan Edits"]["type"] == "duplicate"
    assert by_label["chasedixon22's Fan Edits"]["reason"].startswith("Same Drive folder ID")
    assert by_label["AcyliaEditz Fan Edits"]["type"] == "source"
    # Same URL in two sections is kept twice (source + template).
    fanedit = [e for e in new if e["url"] == "https://fanedit.org/fanedit-search/"]
    assert {e["type"] for e in fanedit} == {"source", "search_template"}
    assert report["duplicates"] == 1


def test_sync_merges_preserved_fields_and_reports_removals():
    old = [
        {"label": "AcyliaEditz Fan Edits",
         "url": "https://drive.google.com/drive/folders/AAA111AAA111AAA111AAA111AAA1",
         "adapter": "drive_folder", "type": "source",
         "note": "Hand-written note."},
        {"label": "Gone Forever",
         "url": "https://example.com/gone",
         "adapter": "website", "type": "source"},
    ]
    entries = parse_doc_entries(FAKE_DOC_HTML)
    new, report = sync_sources(old, entries)
    by_label = {e["label"]: e for e in new}
    assert by_label["AcyliaEditz Fan Edits"]["note"] == "Hand-written note."
    assert report["removed"] == 1
    assert report["sources_dropped"] == ["Gone Forever"]


def test_sync_manifest_fake_fetch_end_to_end():
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return FAKE_DOC_HTML.encode("utf-8")

    def fake_opener(req, timeout=60):
        return FakeResp()

    new, report, ok = sync_manifest([], opener=fake_opener)
    assert ok is True
    assert any(e["label"] == "AcyliaEditz Fan Edits" and e["type"] == "source"
               for e in new)
    assert any(e.get("password") == "J120qPS2wqe" for e in new)


def test_sync_manifest_fetch_failure_falls_back():
    def bad_opener(req, timeout=60):
        raise ConnectionError("boom")

    old = [{"label": "Keep Me", "url": "https://example.com/keep",
            "adapter": "website", "type": "source"}]
    new, report, ok = sync_manifest(old, opener=bad_opener)
    assert ok is False
    assert new == old
    assert "error" in report
