"""核心算法单元测试。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.staypoint import detect_stays, haversine_m, total_distance_m

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def make_points(spec):
    """spec: [(lat, lon, offset_seconds), ...] -> [(lat, lon, datetime), ...]"""
    return [(lat, lon, BASE + timedelta(seconds=t)) for lat, lon, t in spec]


# 测试场景：A 区停留 240s -> 移动到 B 区（~1.1km）停留 120s（不足阈值）->
# 采样间断 1580s -> C 区（与 B 几乎同位置）停留 180s
SCENARIO = make_points(
    [
        (39.900000, 116.400000, 1000),  # 0  A
        (39.900020, 116.400010, 1060),  # 1  A
        (39.899980, 116.399990, 1120),  # 2  A
        (39.900010, 116.400020, 1180),  # 3  A
        (39.899990, 116.399980, 1240),  # 4  A
        (39.910000, 116.400000, 1300),  # 5  B
        (39.910020, 116.400010, 1360),  # 6  B
        (39.909980, 116.399990, 1420),  # 7  B
        (39.910010, 116.400020, 3000),  # 8  C（间断后）
        (39.910000, 116.400000, 3060),  # 9  C
        (39.910020, 116.400010, 3120),  # 10 C
        (39.909990, 116.399990, 3180),  # 11 C
    ]
)


def test_haversine_known_distance():
    # 赤道上 1 经度 ≈ 111195 m
    assert haversine_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(111195.08, rel=1e-4)
    # 同一点距离为 0
    assert haversine_m(39.9, 116.4, 39.9, 116.4) == 0.0


def test_detect_stays_with_gap_splitting():
    stays = detect_stays(
        SCENARIO,
        distance_threshold_m=100.0,
        min_stay_duration_s=180.0,
        max_gap_s=600.0,
    )
    assert len(stays) == 2

    first, second = stays
    # A 区：下标 0-4，时长 240s，中心在簇均值附近
    assert (first.start_index, first.end_index) == (0, 4)
    assert first.duration_s == pytest.approx(240.0)
    assert first.center_lat == pytest.approx(39.9, abs=1e-4)
    assert first.center_lon == pytest.approx(116.4, abs=1e-4)
    # C 区：下标 8-11，时长 180s（间断时间不计入）
    assert (second.start_index, second.end_index) == (8, 11)
    assert second.duration_s == pytest.approx(180.0)


def test_gap_merges_into_false_long_stay_without_max_gap():
    # 不做间断切分时，B、C 距离相近会被合并成一个横跨间断的"伪停留"
    stays = detect_stays(
        SCENARIO,
        distance_threshold_m=100.0,
        min_stay_duration_s=180.0,
        max_gap_s=None,
    )
    assert len(stays) == 2
    assert (stays[1].start_index, stays[1].end_index) == (5, 11)
    assert stays[1].duration_s == pytest.approx(1880.0)


def test_min_duration_filters_short_cluster():
    # 提高最短停留阈值到 300s，A 区（240s）也被过滤
    stays = detect_stays(
        SCENARIO,
        distance_threshold_m=100.0,
        min_stay_duration_s=300.0,
        max_gap_s=600.0,
    )
    assert stays == []


def test_single_point_and_empty():
    assert detect_stays([], 100.0, 300.0) == []
    assert detect_stays(SCENARIO[:1], 100.0, 300.0) == []


def test_total_distance():
    d = total_distance_m(SCENARIO)
    # 主要是 A->B 约 1113m 的跳变，加上簇内小抖动
    assert 1100.0 < d < 1200.0
    assert total_distance_m(SCENARIO[:1]) == 0.0


def test_center_wraps_across_antimeridian():
    # 簇内点在 180° 经线两侧跳号（物理上仍是同一停留）：
    # 朴素经度均值 ~90E 完全错位，圆均值应落在 180°
    points = make_points(
        [
            (0.5, 179.99, 0),
            (0.5, 180.0, 60),
            (0.5, -179.99, 120),
            (0.5, 180.0, 180),
        ]
    )
    stays = detect_stays(
        points,
        distance_threshold_m=3000.0,
        min_stay_duration_s=60.0,
    )
    assert len(stays) == 1
    assert stays[0].center_lat == pytest.approx(0.5)
    assert stays[0].center_lon == pytest.approx(180.0, abs=1e-9)

    # 不跨经线的簇仍为朴素算术均值（行为保持不变）
    normal = make_points(
        [
            (39.9, 116.40000, 0),
            (39.9, 116.40001, 60),
            (39.9, 116.39999, 120),
            (39.9, 116.40000, 180),
        ]
    )
    stays = detect_stays(
        normal, distance_threshold_m=100.0, min_stay_duration_s=60.0
    )
    assert len(stays) == 1
    assert stays[0].center_lon == pytest.approx(116.4, abs=1e-9)
