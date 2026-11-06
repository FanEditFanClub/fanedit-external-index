"""Tests for build.py's Drive junk filter (is_drive_junk)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build import is_drive_junk  # noqa: E402


def _rec(title, link_kind="drive", kind="file"):
    return {"title": title, "editor": "", "original": "",
            "source": "Test", "url": "https://example.com/x",
            "link_kind": link_kind, "kind": kind}


def test_junk_extensions_dropped():
    for title in ("setup.exe", "driver.dll", "poster.jpg", "art.png",
                  "game.gba", "disc.iso", "archive.zip", "file.torrent"):
        assert is_drive_junk(_rec(title)) is True, title


def test_generic_titles_dropped():
    for title in ("Untitled", "INFO", "Edit Info", "readme",
                  "Untitled.mp4", "INFO.txt", "readme.doc", "info.docx"):
        assert is_drive_junk(_rec(title)) is True, title


def test_video_subs_docs_kept():
    for title in ("Blade Runner - Final Cut - Fan Edit.mp4",
                  "Dune - Part Two - Fan Edit.mkv",
                  "Movie - Fan Edit.mov", "Film.avi", "Clip.webm",
                  "subs.srt", "subs.vtt", "subs.ass",
                  "notes.pdf", "edit notes.docx", "changelog.txt",
                  "No Extension At All"):
        assert is_drive_junk(_rec(title)) is False, title


def test_folders_always_kept():
    assert is_drive_junk(_rec("Untitled", kind="folder")) is False
    assert is_drive_junk(_rec("setup.exe", kind="folder")) is False


def test_non_drive_records_untouched():
    assert is_drive_junk(_rec("setup.exe", link_kind="website")) is False
    assert is_drive_junk(_rec("Untitled", link_kind="doc")) is False
    assert is_drive_junk(_rec("", link_kind="website")) is False


def test_empty_title_is_junk():
    assert is_drive_junk(_rec("")) is True
    assert is_drive_junk(_rec("   ")) is True
