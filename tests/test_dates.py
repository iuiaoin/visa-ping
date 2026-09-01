from datetime import date

from visa_ping.scraper import diff_dates, filter_in_range


def test_filter_in_range_inclusive_bounds():
    dates = {date(2026, 9, 14), date(2026, 9, 15), date(2026, 10, 1), date(2026, 12, 31), date(2027, 1, 1)}
    got = filter_in_range(dates, date(2026, 9, 15), date(2026, 12, 31))
    assert got == {date(2026, 9, 15), date(2026, 10, 1), date(2026, 12, 31)}


def test_filter_in_range_empty():
    assert filter_in_range(set(), date(2026, 1, 1), date(2026, 12, 31)) == set()


def test_diff_dates():
    old = {date(2026, 9, 1), date(2026, 9, 2)}
    new = {date(2026, 9, 2), date(2026, 9, 3)}
    added, removed = diff_dates(old, new)
    assert added == {date(2026, 9, 3)}
    assert removed == {date(2026, 9, 1)}


def test_diff_dates_no_change():
    d = {date(2026, 9, 1)}
    assert diff_dates(d, set(d)) == (set(), set())
