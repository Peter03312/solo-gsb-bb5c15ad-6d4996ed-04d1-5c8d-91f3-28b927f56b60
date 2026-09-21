"""围栏路线有序核对算法单元测试。"""
from datetime import datetime, timedelta, timezone

from app.route_check import build_visit_events, check_route
from app.staypoint import StaySegment

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def seg(start_s, duration_s=180.0, lat=0.0, lon=0.0):
    return StaySegment(
        start_index=0,
        end_index=0,
        start_time=BASE + timedelta(seconds=start_s),
        end_time=BASE + timedelta(seconds=start_s + duration_s),
        center_lat=lat,
        center_lon=lon,
    )


def event(zone_id, stay_ids, entry_s, exit_s=None):
    from app.route_check import VisitEvent

    return VisitEvent(
        zone_id=zone_id,
        stay_ids=list(stay_ids),
        entry_time=BASE + timedelta(seconds=entry_s),
        exit_time=BASE + timedelta(seconds=exit_s if exit_s is not None else entry_s + 180),
    )


def test_build_visit_events_skips_misses_and_merges_runs():
    segments = [
        seg(0),       # 1 -> A
        seg(300),     # 2 -> 未命中
        seg(600),     # 3 -> A（与停留 1 之间只夹了未命中停留，仍属同一连续访问）
        seg(900),     # 4 -> B
        seg(1200),    # 5 -> B（连续）
        seg(1500),    # 6 -> A（离开后回来，新事件）
    ]
    assignments = ["A", None, "A", "B", "B", "A"]
    events = build_visit_events(segments, assignments)

    assert [(e.zone_id, e.stay_ids) for e in events] == [
        ("A", [1, 3]),
        ("B", [4, 5]),
        ("A", [6]),
    ]
    # 合并事件的进入 / 离开时间取组内首入 / 末离
    assert events[0].entry_time == BASE + timedelta(seconds=0)
    assert events[0].exit_time == BASE + timedelta(seconds=600 + 180)
    assert events[1].entry_time == BASE + timedelta(seconds=900)
    assert events[1].exit_time == BASE + timedelta(seconds=1200 + 180)


def test_build_visit_events_all_misses_is_empty():
    segments = [seg(0), seg(300)]
    assert build_visit_events(segments, [None, None]) == []
    assert build_visit_events([], []) == []


def test_exact_order_pass():
    events = [event("A", [1], 0), event("B", [2], 1000), event("A", [3], 2000)]
    result = check_route(events, ["A", "B", "A"])

    assert result.overall_pass is True
    assert result.matched_stay_ids == [1, 2, 3]
    assert result.violations == []
    assert [s.step_index for s in result.steps] == [1, 2, 3]
    assert all(s.matched for s in result.steps)
    assert [s.zone_id for s in result.steps] == ["A", "B", "A"]
    assert result.steps[0].gap_s is None
    assert result.steps[1].gap_s == 1000.0
    assert result.steps[2].gap_s == 1000.0


def test_unexpected_then_order_resumes():
    # 路线 A -> B；实际 A -> C -> B：C 为 unexpected，目标 A 已消费、目标 B 继续等待
    events = [event("A", [1], 0), event("C", [2], 500), event("B", [3], 1000)]
    result = check_route(events, ["A", "B"])

    assert result.overall_pass is False
    assert result.matched_stay_ids == [1, 3]
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.kind == "unexpected"
    assert v.zone_id == "C"
    assert v.stay_ids == [2]
    assert v.expected_zone_id == "B"
    assert v.step_index is None
    assert [s.matched for s in result.steps] == [True, True]


def test_missing_step_and_remaining_events_unexpected():
    # 路线 A -> B -> C；实际 A -> C：B missing，C 此时是“非当前目标” unexpected，
    # 路线上的 C 步骤最终也记 missing
    events = [event("A", [1], 0), event("C", [2], 1000)]
    result = check_route(events, ["A", "B", "C"])

    assert result.overall_pass is False
    assert result.matched_stay_ids == [1]
    kinds = [(v.kind, v.zone_id) for v in result.violations]
    assert ("unexpected", "C") in kinds
    assert kinds.count(("missing", "B")) == 1
    assert kinds.count(("missing", "C")) == 1
    # violations 按发生时间排序：先 unexpected（轨迹中的事件），收尾 missing 在最后
    assert kinds[0] == ("unexpected", "C")
    by_step = {s.zone_id: s for s in result.steps}
    assert by_step["A"].matched is True
    assert by_step["B"].matched is False
    assert by_step["C"].matched is False


def test_repeated_zone_needs_separate_visits():
    # A -> B -> A：中间的 B 访问缺失时，两次 A 不能被消费成两个步骤
    events = [event("A", [1, 2], 0), event("A", [3], 3000)]
    result = check_route(events, ["A", "B", "A"])

    # 第一个 A 事件匹配步骤 1；其后的 A 事件相对目标 B 为 unexpected；B 与最后的 A missing
    assert result.matched_stay_ids == [1, 2]
    assert any(v.kind == "unexpected" and v.zone_id == "A" for v in result.violations)
    missing = [v.zone_id for v in result.violations if v.kind == "missing"]
    assert missing == ["B", "A"]


def test_within_gap_pass_and_boundary_inclusive():
    events = [event("A", [1], 0), event("B", [2], 600)]
    # gap 恰好 600，阈值 600 含等号 -> 通过
    result = check_route(events, ["A", "B"], max_step_gap_s=600)
    assert result.overall_pass is True
    assert result.violations == []


def test_gap_timeout_missing_and_late_event_consumed():
    # 时限 600：B 在 900 才出现 -> B missing（deadline=600）；
    # 迟到的正是刚超时的 B，事件一并消费，不再记 unexpected
    events = [event("A", [1], 0), event("B", [2], 900)]
    result = check_route(events, ["A", "B"], max_step_gap_s=600)

    assert result.overall_pass is False
    assert result.matched_stay_ids == [1]
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.kind == "missing"
    assert v.zone_id == "B"
    assert v.step_index == 2
    assert v.deadline_time == BASE + timedelta(seconds=600)
    step_b = result.steps[1]
    assert step_b.matched is False
    assert step_b.entry_time is None


def test_timeout_chained_deadlines():
    # A@0 已匹配；时限 600。B 槽 (0,600]、C 槽 (600,1200] 都缺失；
    # C@1500 到达：先把 B(deadline 600)、C(deadline 1200) 链式结算为 missing，
    # 迟到的 C 正是本轮刚超时的目标 -> 消费该事件（不记 unexpected、C 不回补匹配）
    events = [event("A", [1], 0), event("C", [2], 1500)]
    result = check_route(events, ["A", "B", "C", "D"], max_step_gap_s=600)

    missing = [v for v in result.violations if v.kind == "missing"]
    assert [(v.zone_id, v.deadline_time) for v in missing] == [
        ("B", BASE + timedelta(seconds=600)),
        ("C", BASE + timedelta(seconds=1200)),
        ("D", None),
    ]
    assert result.matched_stay_ids == [1]
    # C 迟到事件被静默消费：没有 unexpected；收尾只剩 D missing
    assert not any(v.kind == "unexpected" for v in result.violations)
    assert result.steps[2].matched is False  # C 步骤不回补为 matched


def test_timeout_unexpected_other_zone_after_expiry():
    # B 在 900 超时（指针推进到 C）后，到达的却是路线之外的 Z：
    # B missing，Z unexpected（当前目标已推进为 C），收尾 C missing
    events = [event("A", [1], 0), event("Z", [2], 900)]
    result = check_route(events, ["A", "B", "C"], max_step_gap_s=600)

    kinds = [(v.kind, v.zone_id) for v in result.violations]
    assert kinds == [
        ("missing", "B"),
        ("unexpected", "Z"),
        ("missing", "C"),
    ]
    unexpected = next(v for v in result.violations if v.kind == "unexpected")
    assert unexpected.expected_zone_id == "C"


def test_first_step_has_no_deadline():
    # 第一个目标没有前序锚点，即使超过时限也不会因时限缺失
    events = [event("A", [1], 10000), event("B", [2], 10100)]
    result = check_route(events, ["A", "B"], max_step_gap_s=600)
    assert result.overall_pass is True


def test_all_steps_missing_when_no_events():
    result = check_route([], ["A", "B"])
    assert result.overall_pass is False
    assert result.matched_stay_ids == []
    assert [v.kind for v in result.violations] == ["missing", "missing"]
    assert all(v.deadline_time is None for v in result.violations)
    assert [s.matched for s in result.steps] == [False, False]


def test_event_after_route_completed_is_unexpected():
    events = [event("A", [1], 0), event("B", [2], 600), event("C", [3], 900)]
    result = check_route(events, ["A", "B"])
    unexpected = [v for v in result.violations if v.kind == "unexpected"]
    assert len(unexpected) == 1
    assert unexpected[0].zone_id == "C"
    assert unexpected[0].expected_zone_id is None  # 路线已走完


def test_merged_event_consumes_one_step():
    # A 的连续停留合并为一次访问（stay_ids [1,2]），整体消费单个 A 步骤
    events = [event("A", [1, 2], 0, exit_s=600 + 180), event("B", [3], 2000)]
    result = check_route(events, ["A", "B"])
    assert result.overall_pass is True
    assert result.matched_stay_ids == [1, 2, 3]
    step_a = result.steps[0]
    assert step_a.stay_ids == [1, 2]
    assert step_a.exit_time == BASE + timedelta(seconds=600 + 180)
