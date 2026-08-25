from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import requests
from bs4 import BeautifulSoup

import airfryer_rankings.fixture_capture as capture
from airfryer_rankings.models import SourceConfig


def test_sanitize_fixture_html_keeps_evidence_without_active_markup() -> None:
    source = "https://publisher.example/recipe"
    raw = """
    <html><head><title>Unsafe & Recipe</title>
    <link rel="canonical" href='https://publisher.example/recipe?x=&quot; onload=&quot;alert(1)'>
    <script type="application/ld+json">
      {"@type":"Recipe","name":"<img src=x onerror=alert(1)>",
       "recipeIngredient":["1","2","3","4","5","6","7","8","9"],
       "recipeInstructions":["a","b","c","d","e"]}
    </script></head><body>
      <span itemprop="ratingValue" onclick="alert(1)"><b>4.9</b><script>alert(2)</script></span>
    </body></html>
    """

    sanitized = capture.sanitize_fixture_html(raw, source)
    soup = BeautifulSoup(sanitized, "lxml")
    payload = json.loads(soup.find("script", type="application/ld+json").string)

    assert payload["name"] == "<img src=x onerror=alert(1)>"
    assert len(payload["recipeIngredient"]) == 8
    assert len(payload["recipeInstructions"]) == 4
    assert "onclick=" not in sanitized
    assert "<img src=x" not in sanitized
    assert len(soup.find_all("script")) == 1
    assert soup.find(itemprop="ratingValue").get_text(" ", strip=True) == "4.9"


def test_capture_candidate_fixtures_classifies_every_source(monkeypatch, tmp_path: Path) -> None:
    sources = [SourceConfig(f"{name}.example") for name in ("good", "denied", "robots", "failed", "missing")]
    state = {
        "recipes": {
            name: {
                "source": f"{name}.example",
                "url": f"https://{name}.example/recipe",
                "rating_count": index,
            }
            for index, name in enumerate(("good", "denied", "robots", "failed"), start=1)
        }
    }

    class Parser:
        def __init__(self, domain: str) -> None:
            self.domain = domain

        def can_fetch(self, *_args) -> bool:
            if self.domain == "robots.example":
                raise ValueError("invalid robots policy")
            return self.domain != "denied.example"

    def fake_get(_session, url: str, _timeout: int):
        if "failed.example" in url:
            raise requests.Timeout("offline")
        return SimpleNamespace(text='<html><span itemprop="ratingValue">5</span></html>')

    monkeypatch.setattr(capture, "load_sources", lambda *_: sources)
    monkeypatch.setattr(capture, "load_state", lambda *_: state)
    monkeypatch.setattr(capture, "make_session", object)
    monkeypatch.setattr(
        capture,
        "robots_and_sitemaps",
        lambda _session, cfg: (Parser(cfg.domain), [], "", "ok"),
    )
    monkeypatch.setattr(capture, "get", fake_get)

    result = capture.capture_candidate_fixtures("sources.yaml", "state.json", str(tmp_path))

    assert [row["source"] for row in result["captured"]] == ["good.example"]
    assert {row["reason"] for row in result["failed"]} == {
        "robots_denied",
        "robots_error:ValueError",
        "Timeout",
    }
    assert result["missing_representative"] == ["missing.example"]
    assert (tmp_path / "good_example.html").is_file()
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == result


def test_fixture_capture_main_forwards_limit(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        capture,
        "capture_candidate_fixtures",
        lambda *args: {"captured": [args], "failed": [], "missing_representative": []},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["fixture-capture", "--output", str(tmp_path), "--max-sources", "1"],
    )

    capture.main()

    assert json.loads(capsys.readouterr().out) == {
        "captured": 1,
        "failed": 0,
        "missing_representative": 0,
    }
