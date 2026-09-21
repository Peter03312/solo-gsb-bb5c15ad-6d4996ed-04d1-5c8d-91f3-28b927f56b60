"""热点归并算法单元测试。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.hotspot import find_hotspots
from app.staypoint import StaySegment

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def seg(lat, lon, duration_s=300.0, start_s=0, start_index=0):
    """构造单段停留（end_index 默认等于 start_index）。"""
    return StaySegment(
        start_index=start_index,
        end_index=start_index,
        start_time=BASE + timedelta(seconds=start_s),
        end_time=BASE + timedelta(seconds=start_s + duration_s),
        center_lat=lat,
        center_lon=lon,
    )


def test_chain_is_transitive_single_hotspot():
    # 甲(39.9000) 邻 乙(39.9018，约 200m) 邻 丙(39.9036)；甲丙相距约 400m > 300m
    segments = [
        seg(39.9000, 116.4, start_s=0),
        seg(39.9018, 116.4, start_s=3600),
        seg(39.9036, 116.4, start_s=7200),
    ]
    hotspots, assignment = find_hotspots(segments, 300.0, 2)
    assert len(hotspots) == 1
    hs = hotspots[0]
    assert hs.hotspot_id == 1
    assert hs.visit_count == 3
    assert hs.member_indices == [0, 1, 2]
    assert assignment == {0: 1, 1: 1, 2: 1}


def test_multiple_components_and_noise():
    # H(39.900) 两处访问；W(39.920) 两处访问（H、W 相距约 2.2km）；
    # X(40.000) 仅一次访问 -> 噪声
    segments = [
        seg(39.900, 116.4, duration_s=300.0, start_s=0),
        seg(39.920, 116.4, duration_s=100.0, start_s=3600),
        seg(40.000, 116.4, duration_s=200.0, start_s=7200),
        seg(39.900, 116.4, duration_s=600.0, start_s=10800),
        seg(39.920, 116.4, duration_s=400.0, start_s=14400),
    ]
    hotspots, assignment = find_hotspots(segments, 200.0, 2)

    assert [h.hotspot_id for h in hotspots] == [1, 2]
    home, work = hotspots

    assert home.visit_count == 2
    assert home.member_indices == [0, 3]
    assert home.total_duration_s == pytest.approx(900.0)
    assert work.visit_count == 2
    assert work.member_indices == [1, 4]
    assert work.total_duration_s == pytest.approx(500.0)

    # 一次性到访点为噪声
    assert assignment[2] is None


def test_min_visits_threshold_not_met_all_noise():
    segments = [
        seg(39.900, 116.4, start_s=0),
        seg(39.900, 116.4, start_s=600),
    ]
    hotspots, assignment = find_hotspots(segments, 200.0, 3)
    assert hotspots == []
    assert all(hotspot_id is None for hotspot_id in assignment.values())


def test_radius_boundary_is_inclusive():
    # 间距 200.0001m：200m 不连；201m 连
    segments = [
        seg(39.900000, 116.4, start_s=0),
        seg(39.901800, 116.4, start_s=600),
    ]
    hotspots, _ = find_hotspots(segments, 200.0, 2)
    assert hotspots == []
    hotspots, _ = find_hotspots(segments, 201.0, 2)
    assert len(hotspots) == 1
    assert hotspots[0].member_indices == [0, 1]


def test_duration_weighted_center():
    segments = [
        seg(39.900, 116.4, duration_s=300.0, start_s=0),
        seg(39.902, 116.4, duration_s=900.0, start_s=3600),
    ]
    hotspots, _ = find_hotspots(segments, 500.0, 2)
    assert len(hotspots) == 1
    # (39.900*300 + 39.902*900) / 1200 = 39.9015
    assert hotspots[0].center_lat == pytest.approx(39.9015, abs=1e-9)
    assert hotspots[0].center_lon == pytest.approx(116.4, abs=1e-9)
    assert hotspots[0].first_visit_time == BASE
    assert hotspots[0].last_visit_time == BASE + timedelta(seconds=3600 + 900)


def test_hotspot_ids_ordered_by_earliest_visit_time():
    # 分量 W 在索引 0、2 但首次访问在 3000s；分量 H 在索引 1、3 但首次访问在 0s
    segments = [
        seg(39.920, 116.4, start_s=3000),
        seg(39.900, 116.4, start_s=0),
        seg(39.920, 116.4, start_s=6000),
        seg(39.900, 116.4, start_s=9000),
    ]
    hotspots, assignment = find_hotspots(segments, 200.0, 2)
    assert [h.hotspot_id for h in hotspots] == [1, 2]
    assert hotspots[0].member_indices == [1, 3]
    assert hotspots[1].member_indices == [0, 2]
    assert assignment == {0: 2, 1: 1, 2: 2, 3: 1}


def test_result_independent_of_traversal_order():
    # 链式（传递）场景，打乱输入顺序后分量划分必须一致。
    # 半径 150m：A-B、B-C 各约 111m 连边，A-C 约 222m 不连边——仅能靠传递合并
    segments = [
        seg(39.900, 116.4, duration_s=300.0, start_s=0),
        seg(39.901, 116.4, duration_s=100.0, start_s=3600),
        seg(39.902, 116.4, duration_s=200.0, start_s=7200),
        seg(39.950, 116.4, duration_s=400.0, start_s=1800),
        seg(39.951, 116.4, duration_s=500.0, start_s=5400),
    ]

    def partitions(segs):
        _, assignment = find_hotspots(segs, 150.0, 1)
        groups = {}
        for idx, hid in assignment.items():
            groups.setdefault(hid, set()).add(idx)
        return frozenset(
            frozenset(members) for members in groups.values()
        )

    expected = partitions(segments)
    assert expected == frozenset({frozenset({0, 1, 2}), frozenset({3, 4})})

    permutations = [
        [2, 0, 4, 1, 3],
        [4, 3, 2, 1, 0],
        [3, 1, 0, 4, 2],
    ]
    for order in permutations:
        shuffled = [segments[i] for i in order]
        _, shuffled_assignment = find_hotspots(shuffled, 150.0, 1)
        shuffled_groups = {}
        for idx, hid in shuffled_assignment.items():
            shuffled_groups.setdefault(hid, set()).add(order[idx])
        shuffled_partitions = frozenset(
            frozenset(members) for members in shuffled_groups.values()
        )
        assert shuffled_partitions == expected

    # 颠倒输入顺序后，热点仍按最早访问时间编号（链 -> 1, W -> 2）
    reversed_segments = [
        seg(39.951, 116.4, duration_s=500.0, start_s=5400),  # 0 -> W
        seg(39.950, 116.4, duration_s=400.0, start_s=1800),  # 1 -> W
        seg(39.902, 116.4, duration_s=200.0, start_s=7200),  # 2 -> 链 C
        seg(39.901, 116.4, duration_s=100.0, start_s=3600),  # 3 -> 链 B
        seg(39.900, 116.4, duration_s=300.0, start_s=0),     # 4 -> 链 A
    ]
    hotspots, assignment = find_hotspots(reversed_segments, 150.0, 2)
    # 成员下标按升序输出；编号按最早访问时间：链首次访问 0s，W 首次访问 1800s
    assert [h.member_indices for h in hotspots] == [[2, 3, 4], [0, 1]]
    assert assignment == {0: 2, 1: 2, 2: 1, 3: 1, 4: 1}


def test_zero_duration_falls_back_to_unweighted_center():
    segments = [
        seg(39.900, 116.4, duration_s=0.0, start_s=0),
        seg(39.902, 116.4, duration_s=0.0, start_s=600),
    ]
    hotspots, _ = find_hotspots(segments, 500.0, 2)
    assert len(hotspots) == 1
    assert hotspots[0].center_lat == pytest.approx(39.901, abs=1e-9)
    assert hotspots[0].total_duration_s == 0.0


def test_empty_segments():
    hotspots, assignment = find_hotspots([], 200.0, 2)
    assert hotspots == []
    assert assignment == {}
