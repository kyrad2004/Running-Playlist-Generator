"""The .env failure modes python-dotenv reports poorly or not at all."""

import codecs

import pytest

from rpg import envcheck


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Point envcheck at a throwaway directory instead of the real repo root."""
    monkeypatch.setattr(envcheck, "REPO_ROOT", tmp_path)
    return tmp_path


GOOD = b"SPOTIFY_CLIENT_ID=abc123\nSTRAVA_CLIENT_ID=1\nSTRAVA_CLIENT_SECRET=s\n"


def summaries(problems):
    return " ".join(p.summary for p in problems)


def test_clean_env_reports_nothing(fake_repo):
    (fake_repo / ".env").write_bytes(GOOD)
    assert envcheck.diagnose(("SPOTIFY_CLIENT_ID",)) == []


def test_quotes_and_export_and_crlf_are_all_fine(fake_repo):
    """These look wrong but python-dotenv handles them — don't cry wolf."""
    (fake_repo / ".env").write_bytes(
        b'export SPOTIFY_CLIENT_ID="abc123"\r\nSTRAVA_CLIENT_ID = 1\r\n'
    )
    assert envcheck.diagnose(("SPOTIFY_CLIENT_ID", "STRAVA_CLIENT_ID")) == []


def test_missing_file(fake_repo):
    problems = envcheck.diagnose()
    assert "No .env file" in problems[0].summary


def test_missing_file_notices_dot_env_txt(fake_repo):
    """Windows hides known extensions, so '.env' in Explorer is often '.env.txt'."""
    (fake_repo / ".env.txt").write_bytes(GOOD)
    problems = envcheck.diagnose()
    assert ".env.txt" in problems[0].summary
    assert "Rename" in problems[0].fix


def test_missing_file_notices_only_the_template_exists(fake_repo):
    (fake_repo / ".env.example").write_bytes(b"SPOTIFY_CLIENT_ID=\n")
    problems = envcheck.diagnose()
    assert "never read" in problems[0].summary
    assert "cp .env.example .env" in problems[0].fix


def test_utf8_bom_is_detected(fake_repo):
    """dotenv reads the first key as '\\ufeffSPOTIFY_CLIENT_ID' and silently misses it."""
    (fake_repo / ".env").write_bytes(codecs.BOM_UTF8 + GOOD)
    assert "byte-order mark" in summaries(envcheck.diagnose())


def test_utf16_is_detected(fake_repo):
    (fake_repo / ".env").write_bytes("SPOTIFY_CLIENT_ID=abc123\n".encode("utf-16"))
    assert "UTF-16" in summaries(envcheck.diagnose())


def test_smart_quotes_are_detected(fake_repo):
    (fake_repo / ".env").write_bytes("SPOTIFY_CLIENT_ID=“abc123”\n".encode())
    assert "smart quotes" in summaries(envcheck.diagnose())


def test_colon_syntax_is_detected(fake_repo):
    (fake_repo / ".env").write_bytes(b"SPOTIFY_CLIENT_ID: abc123\n")
    assert "colon" in summaries(envcheck.diagnose())


def test_editing_the_template_instead_of_env_is_detected(fake_repo):
    (fake_repo / ".env").write_bytes(b"SPOTIFY_CLIENT_ID=\n")
    (fake_repo / ".env.example").write_bytes(b"SPOTIFY_CLIENT_ID=abc123\n")
    assert "You edited the template" in envcheck.diagnose()[0].fix


def test_empty_file(fake_repo):
    (fake_repo / ".env").write_bytes(b"\n\n")
    assert "is empty" in summaries(envcheck.diagnose())


def test_key_present_but_blank(fake_repo):
    (fake_repo / ".env").write_bytes(b"SPOTIFY_CLIENT_ID=\n")
    assert "parses as empty" in summaries(envcheck.diagnose())


def test_comments_are_not_flagged(fake_repo):
    (fake_repo / ".env").write_bytes(b"# a note: with a colon\nSPOTIFY_CLIENT_ID=abc123\n")
    assert envcheck.diagnose(("SPOTIFY_CLIENT_ID",)) == []


def test_format_problems_renders_summary_and_fix(fake_repo):
    (fake_repo / ".env").write_bytes(b"SPOTIFY_CLIENT_ID: abc\n")
    text = envcheck.format_problems(envcheck.diagnose())
    assert "1." in text and "→" in text
