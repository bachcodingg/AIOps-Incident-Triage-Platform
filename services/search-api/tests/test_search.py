import pytest

from app.search import Index, edit_distance, search, tokenize

DOCS = {
    1: {"name": "Gaming Laptop 15", "category": "laptops", "description": "RTX graphics, 16 GB RAM", "price": 1299.0},
    2: {"name": "Ultrabook 13", "category": "laptops", "description": "Light laptop for travel", "price": 999.0},
    3: {"name": "Noise Cancelling Headphones", "category": "audio", "description": "Wireless, 30 h battery", "price": 249.0},
    4: {"name": "Smartphone Pro", "category": "phones", "description": "OLED screen and great camera", "price": 899.0},
}


@pytest.fixture
def index() -> Index:
    return Index.build(DOCS)


def test_tokenize_lowercases_and_drops_stopwords():
    assert tokenize("The Best Laptop for GAMING, 2026!") == ["best", "laptop", "gaming", "2026"]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [("laptop", "laptop", 0), ("lapotp", "laptop", 2), ("lptop", "laptop", 1), ("phone", "phones", 1)],
)
def test_edit_distance(a, b, expected):
    assert edit_distance(a, b, limit=3) == expected


def test_edit_distance_stops_past_limit():
    assert edit_distance("headphones", "tv", limit=2) == 3


def test_name_match_outranks_description_match(index):
    result = search(index, "laptop")
    assert [r["id"] for r in result.results][:2] == [1, 2]
    assert result.total == 2


def test_typo_is_corrected(index):
    result = search(index, "hedphones")
    assert result.corrected_query == "headphones"
    assert result.results[0]["id"] == 3


def test_short_tokens_are_not_corrected(index):
    # "pc" must not be "corrected" into some other two-letter term.
    assert search(index, "pc").corrected_query is None


def test_no_correction_when_query_is_known(index):
    assert search(index, "camera").corrected_query is None


def test_unknown_terms_return_nothing(index):
    result = search(index, "zzzzzzzz")
    assert result.total == 0
    assert result.results == []


def test_limit_caps_results_but_not_total(index):
    result = search(index, "laptop", limit=1)
    assert len(result.results) == 1
    assert result.total == 2


def test_empty_index():
    assert search(Index.build({}), "anything").total == 0
