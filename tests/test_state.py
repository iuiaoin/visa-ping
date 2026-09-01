import random
from datetime import date

from visa_ping.config import MonitorCfg
from visa_ping.monitor import MonitorState, Pacer, load_state, save_state


def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = MonitorState(session_alert_sent=True)
    state.set_known_dates({date(2026, 10, 5), date(2026, 9, 1)})
    save_state(path, state)

    loaded = load_state(path)
    assert loaded.known_in_range_dates == ["2026-09-01", "2026-10-05"]  # sorted
    assert loaded.known_dates == {date(2026, 9, 1), date(2026, 10, 5)}
    assert loaded.session_alert_sent is True


def test_missing_state_file(tmp_path):
    state = load_state(tmp_path / "nope.json")
    assert state.known_in_range_dates == []
    assert state.session_alert_sent is False


def test_corrupt_state_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = load_state(path)
    assert state.known_in_range_dates == []


def test_pacer_bounds_and_rest():
    cfg = MonitorCfg(
        check_interval_min_seconds=100,
        check_interval_max_seconds=200,
        rest_min_seconds=400,
        rest_max_seconds=500,
        checks_before_rest_min=2,
        checks_before_rest_max=3,
    )
    pacer = Pacer(cfg, rng=random.Random(42))
    waits = [pacer.next_wait_seconds() for _ in range(50)]
    rests = [w for w in waits if w >= 400]
    normals = [w for w in waits if w < 400]
    assert all(100 <= w <= 200 for w in normals)
    assert all(400 <= w <= 500 for w in rests)
    # With thresholds of 2-3, roughly a third to a half of waits are rests.
    assert rests, "expected at least one long rest"
    assert normals, "expected normal-interval waits too"
