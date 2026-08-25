from bs4 import BeautifulSoup

from airfryer_rankings.evidence import (
    _author_name,
    _canonical_url,
    _element_number,
    _evidence_score,
    _image_url,
    _instruction_texts,
    _parse_histogram,
    _parse_number,
    jsonld_objects,
    visible_rating_evidence,
)
from airfryer_rankings.extract import extract_recipe_from_html
from airfryer_rankings.models import SourceConfig


def test_jsonld_nested_graph_and_main_entity_ignore_invalid_json():
    soup = BeautifulSoup(
        """
        <html><head>
          <script type="application/ld+json">not-json</script>
          <script type="application/ld+json">
          {"@graph":[{"@type":"Thing","name":"graph"}],
           "mainEntity":{"@type":"Recipe","name":"nested"}}
          </script>
        </head></html>
        """,
        "lxml",
    )
    objects = list(jsonld_objects(soup))
    names = {obj.get("name") for obj in objects}
    assert {"graph", "nested"}.issubset(names)


def test_author_instruction_and_image_shape_helpers():
    assert _author_name({"author": " Alice "}) == "Alice"
    assert _author_name({"author": {"name": "Bob"}}) == "Bob"
    assert _author_name({"author": ["Alice", {"name": "Bob"}, 3]}) == "Alice, Bob"
    assert _author_name({}) == ""

    instructions = _instruction_texts(
        {
            "recipeInstructions": [
                " First step ",
                {"name": "Second step"},
                {"itemListElement": [{"text": "Third step"}, {"name": "ignored"}]},
            ]
        }
    )
    assert instructions == ("First step", "Second step", "Third step")
    assert _instruction_texts({"recipeInstructions": "One step"}) == ("One step",)

    assert _image_url({"image": " https://example.com/a.jpg "}).endswith("a.jpg")
    assert _image_url({"image": [{"contentUrl": "https://example.com/b.jpg"}]}).endswith("b.jpg")
    assert _image_url({"image": {"url": "https://example.com/c.jpg"}}).endswith("c.jpg")
    assert _image_url({}) == ""


def test_number_histogram_and_element_parsing_edges():
    assert _parse_number(None) is None
    assert _parse_number(12) == 12.0
    assert _parse_number("1,234 reviews") == 1234.0
    assert _parse_number("none") is None

    assert _parse_histogram({"ratingHistogram": {"5": "80", "4 stars": "20", "9": 1}}) == {
        "5": 80,
        "4": 20,
    }
    assert _parse_histogram(
        {
            "ratingDistribution": [
                {"ratingValue": 5, "count": 7},
                {"star": "4", "ratingCount": "3"},
                "bad",
            ]
        }
    ) == {"5": 7, "4": 3}

    soup = BeautifulSoup('<span data-rating="4.7">ignored</span><span>2,345 ratings</span>', "lxml")
    spans = soup.find_all("span")
    assert _element_number(spans[0]) == 4.7
    assert _element_number(spans[1]) == 2345.0
    assert _element_number(None) is None


def test_visible_evidence_selectors_microdata_and_evidence_scoring():
    soup = BeautifulSoup(
        """
        <div class="score" aria-label="4.82 out of 5"></div>
        <div class="count" data-value="321"></div>
        <span itemprop="ratingValue" content="4.7"></span>
        <span itemprop="reviewCount">200 reviews</span>
        """,
        "lxml",
    )
    rating, count = visible_rating_evidence(
        soup, SourceConfig("example.com", rating_selector=".score", count_selector=".count")
    )
    assert rating == 4.82
    assert count == 321

    rating, count = visible_rating_evidence(soup, SourceConfig("example.com"))
    assert rating == 4.7
    assert count == 200

    assert _evidence_score(4.8, 100, None, None) == (0.65, "schema_only", "jsonld")
    assert _evidence_score(4.8, 100, 4.76, 105) == (1.0, "verified", "jsonld+visible")
    assert _evidence_score(4.8, 100, 4.2, 100)[1] == "conflict"
    assert _evidence_score(4.8, 100, 4.8, 150)[1] == "conflict"


def test_relative_canonical_and_visible_only_extraction_fallback():
    soup = BeautifulSoup('<link rel="canonical" href="/recipe/test">', "lxml")
    assert _canonical_url(soup, "https://example.com/original") == "https://example.com/recipe/test"
    assert (
        _canonical_url(BeautifulSoup("<html></html>", "lxml"), "https://example.com/fallback")
        == "https://example.com/fallback"
    )

    html = """
    <html><head><title>Visible Air Fryer Test</title></head><body>
      <span itemprop="ratingValue" content="4.6"></span>
      <span itemprop="reviewCount" content="87"></span>
    </body></html>
    """
    row, meta = extract_recipe_from_html(html, "https://example.com/visible", "example.com")
    assert row is not None
    assert row.evidence_status == "visible_only"
    assert row.rating_count == 87
    assert row.extraction_method == "visible_microdata"
    assert meta["page_hash"]


def test_extraction_rejects_malformed_structured_rating_scale():
    html = """
    <html><head><title>Bad Rating</title>
      <script type="application/ld+json">
      {"@type":"Recipe","name":"Bad Rating","recipeIngredient":["potato"],
       "aggregateRating":{"ratingValue":"9","ratingCount":"10","bestRating":"5"}}
      </script>
    </head><body></body></html>
    """
    row, meta = extract_recipe_from_html(html, "https://example.com/bad", "example.com")
    assert row is None
    assert "malformed_rating_scale" in meta["issues"]
