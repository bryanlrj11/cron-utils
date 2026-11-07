"""Reference books are fetched with TAB but stored apart, so TAB's thin-snapshot checks hold."""

from jobs.odds import MARKETS, STANDARD_MARKETS, markets_for, split_payload


def _payload(*keys: str) -> dict:
    return {"id": "e1", "bookmakers": [{"key": key, "markets": []} for key in keys]}


def test_tab_stays_alone_in_the_original_snapshot():
    tab, reference = split_payload(_payload("tab", "pinnacle", "fanduel"))

    assert [b["key"] for b in tab["bookmakers"]] == ["tab"]
    assert [b["key"] for b in reference["bookmakers"]] == ["pinnacle", "fanduel"]


def test_a_snapshot_with_only_reference_books_still_looks_thin_for_tab():
    tab, reference = split_payload(_payload("pinnacle"))

    assert tab["bookmakers"] == []
    assert reference is not None


def test_no_reference_snapshot_is_made_when_only_tab_answers():
    _, reference = split_payload(_payload("tab"))

    assert reference is None


def test_event_fields_are_kept_on_both_halves():
    tab, reference = split_payload(_payload("tab", "pinnacle"))

    assert tab["id"] == reference["id"] == "e1"


def test_only_close_runs_pay_for_the_two_sided_main_markets():
    assert markets_for("open") == MARKETS
    assert markets_for("close") == MARKETS + STANDARD_MARKETS
