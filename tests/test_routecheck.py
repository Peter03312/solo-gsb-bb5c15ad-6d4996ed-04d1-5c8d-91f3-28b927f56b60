"""路线顺序核对算法单元测试。"""
from datetime import datetime, timedelta, timezone

from app.routecheck import ZoneEvent, build_zone_events, check_route
from app.staypoint import StaySegment

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def seg(start_s=0, duration_s=180.0):
    return StaySegment(
        start_index=0,
        end_index=0,
        start_time=BASE + timedelta(seconds=start_s),
        end_time=BASE + timedelta(seconds=start_s + duration_s),
        center_lat=39.9,
        center_lon=116.4,
    )


def ev(zone, stay_id, start_s):
    return ZoneEvent(
        zone_id=zone, stay_id=stay_id, enter_time=BASE + timedelta(seconds=start_s)
    )


# --- build_zone_events：事件流构建 ---


def test_events_skip_unmatched_and_collapse_consecutive():
    segments = [seg(i * 600) for i in range(5)]
    assignments = ["a", None, "a", "b", "b"]
    events = build_zone_events(segments, assignments)
    # 未命中（None）跳过；同一围栏连续命中（含隔着未命中停留）折叠，取首段停留
    assert [(e.zone_id, e.stay_id) for e in events] == [("a", 1), ("b", 4)]
    assert events[0].enter_time == segments[0].start_time
    assert events[1].enter_time == segments[3].start_time


def test_events_empty_when_no_stays_or_no_hits():
    assert build_zone_events([], []) == []
    assert build_zone_events([seg(0)], [None]) == []


# --- check_route：顺序匹配 ---


def test_exact_match_passes_with_duplicate_targets():
    events = [ev("home", 1, 0), ev("work", 2, 3000), ev("home", 3, 6000)]
    result = check_route(events, ["home", "work", "home"], None, BASE)
    assert result.overall_pass
    assert result.matched_stay_ids == [1, 2, 3]
    assert [s.status for s in result.steps] == ["matched", "matched", "matched"]
    assert [s.stay_id for s in result.steps] == [1, 2, 3]
    assert result.violations == []


def test_unexpected_event_recorded_and_not_consumed():
    # 出现非当前目标：记 unexpected 并丢弃，后续目标仍可匹配
    events = [ev("a", 1, 0), ev("x", 2, 100), ev("b", 3, 200)]
    result = check_route(events, ["a", "b"], None, BASE)
    assert not result.overall_pass
    assert result.matched_stay_ids == [1, 3]
    assert [s.status for s in result.steps] == ["matched", "matched"]
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.type == "unexpected"
    assert v.zone_id == "x"
    assert v.stay_id == 2
    assert v.expected_zone_id == "b"


def test_unexpected_after_route_complete():
    events = [ev("a", 1, 0), ev("b", 2, 100)]
    result = check_route(events, ["a"], None, BASE)
    assert not result.overall_pass
    v = result.violations[0]
    assert v.type == "unexpected"
    assert v.zone_id == "b"
    assert v.expected_zone_id is None  # 路线已完成


def test_missing_when_target_never_appears():
    events = [ev("a", 1, 0)]
    result = check_route(events, ["a", "b"], None, BASE)
    assert not result.overall_pass
    assert result.matched_stay_ids == [1]
    assert [s.status for s in result.steps] == ["matched", "missing"]
    assert result.steps[1].stay_id is None
    assert result.steps[1].enter_time is None
    v = result.violations[0]
    assert v.type == "missing"
    assert v.zone_id == "b"
    assert v.step_index == 2
    assert v.deadline_time is None  # 未设 max_step_gap_s


def test_empty_targets():
    # 空路线 + 无事件：通过；空路线 + 有到访：全部 unexpected
    result = check_route([], [], None, BASE)
    assert result.overall_pass
    assert result.steps == [] and result.violations == []

    result = check_route([ev("a", 1, 0)], [], None, BASE)
    assert not result.overall_pass
    assert result.violations[0].type == "unexpected"
    assert result.violations[0].expected_zone_id is None


def test_duplicate_target_needs_two_separate_visits():
    # 连续同围栏停留已折叠为一次事件，第二个相同目标只能记 missing
    events = [ev("a", 1, 0)]
    result = check_route(events, ["a", "a"], None, BASE)
    assert not result.overall_pass
    assert [s.status for s in result.steps] == ["matched", "missing"]


# --- check_route：max_step_gap_s 时限 ---


def test_timeout_marks_missing_and_shifts_deadline():
    # gap=100：a@10 匹配；b 须在 110 前出现，超时记 missing 且时限顺延至 210；c@190 仍匹配
    events = [ev("a", 1, 10), ev("c", 2, 190)]
    result = check_route(events, ["a", "b", "c"], 100.0, BASE)
    assert not result.overall_pass
    assert [s.status for s in result.steps] == ["matched", "missing", "matched"]
    assert result.matched_stay_ids == [1, 2]
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.type == "missing"
    assert v.zone_id == "b"
    assert v.step_index == 2
    assert v.deadline_time == BASE + timedelta(seconds=110)


def test_timeout_cascades_then_events_become_unexpected():
    # gap=100：首目标锚定轨迹起点 BASE；a@250 到达时 b、c 依次超时，a 本身成为 unexpected
    events = [ev("a", 1, 250)]
    result = check_route(events, ["b", "c"], 100.0, BASE)
    assert not result.overall_pass
    assert [s.status for s in result.steps] == ["missing", "missing"]
    kinds = [(v.type, v.zone_id) for v in result.violations]
    assert kinds == [("missing", "b"), ("missing", "c"), ("unexpected", "a")]
    assert result.violations[0].deadline_time == BASE + timedelta(seconds=100)
    assert result.violations[1].deadline_time == BASE + timedelta(seconds=200)


def test_deadline_boundary_is_inclusive():
    events = [ev("a", 1, 100)]
    result = check_route(events, ["a"], 100.0, BASE)
    assert result.overall_pass
    assert result.matched_stay_ids == [1]


def test_first_target_deadline_anchors_at_track_start():
    # 首个目标以轨迹首点时间为锚：a@60 超出 gap=50 的时限
    events = [ev("a", 1, 60)]
    result = check_route(events, ["a"], 50.0, BASE)
    assert not result.overall_pass
    assert [s.status for s in result.steps] == ["missing"]
    kinds = [(v.type, v.zone_id) for v in result.violations]
    assert kinds == [("missing", "a"), ("unexpected", "a")]
    assert result.violations[0].deadline_time == BASE + timedelta(seconds=50)


def test_unexpected_event_does_not_reset_deadline_anchor():
    # unexpected 不影响时限锚点：b 的时限仍按 a 的匹配时间计算
    events = [ev("a", 1, 10), ev("x", 2, 50), ev("b", 3, 120)]
    result = check_route(events, ["a", "b"], 100.0, BASE)
    assert not result.overall_pass
    kinds = [(v.type, v.zone_id) for v in result.violations]
    assert kinds == [("unexpected", "x"), ("missing", "b"), ("unexpected", "b")]
    assert result.violations[1].deadline_time == BASE + timedelta(seconds=110)
