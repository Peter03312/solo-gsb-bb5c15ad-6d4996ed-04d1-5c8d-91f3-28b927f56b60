"""围栏点面归属与汇总算法单元测试。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.geofence import (
    assign_zones,
    build_fence,
    point_in_fence,
    summarize_fences,
)
from app.staypoint import StaySegment

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

# 普通方框：经度 0..10，纬度 0..10
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
}
# 带洞方框：外环 0..10，洞 4..6
SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ],
}
# 跨 180° 经线小方块：展开后经度 179.9..180.1，纬度 0..1（正向编码）
ANTIMERIDIAN = {
    "type": "Polygon",
    "coordinates": [
        [[179.9, 0.0], [-179.9, 0.0], [-179.9, 1.0], [179.9, 1.0], [179.9, 0.0]]
    ],
}
# 同一方块反向编码：展开后经度 -180.1..-179.9
ANTIMERIDIAN_REV = {
    "type": "Polygon",
    "coordinates": [
        [[-179.9, 0.0], [179.9, 0.0], [179.9, 1.0], [-179.9, 1.0], [-179.9, 0.0]]
    ],
}
# MultiPolygon：一块普通区域 + 一块跨 180° 区域
MULTI = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
        [
            [
                [179.9, 0.0],
                [-179.9, 0.0],
                [-179.9, 1.0],
                [179.9, 1.0],
                [179.9, 0.0],
            ]
        ],
    ],
}


def seg(lat, lon, duration_s=300.0, start_s=0):
    return StaySegment(
        start_index=0,
        end_index=0,
        start_time=BASE + timedelta(seconds=start_s),
        end_time=BASE + timedelta(seconds=start_s + duration_s),
        center_lat=lat,
        center_lon=lon,
    )


def test_plain_polygon_inside_and_outside():
    fence = build_fence("z", 0, SQUARE)
    assert point_in_fence(5.0, 5.0, fence)
    assert point_in_fence(0.0001, 0.0001, fence)
    assert not point_in_fence(-1.0, 5.0, fence)
    assert not point_in_fence(5.0, 10.5, fence)
    assert not point_in_fence(0.0, 20.0, fence)


def test_outer_ring_boundary_is_hit():
    fence = build_fence("z", 0, SQUARE)
    # 四个顶点
    for lon, lat in [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]:
        assert point_in_fence(lat, lon, fence)
    # 边的中点
    assert point_in_fence(0.0, 5.0, fence)   # 南边
    assert point_in_fence(10.0, 5.0, fence)  # 北边
    assert point_in_fence(5.0, 0.0, fence)   # 西边
    assert point_in_fence(5.0, 10.0, fence)  # 东边


def test_hole_interior_and_boundary_are_miss():
    fence = build_fence("z", 0, SQUARE_WITH_HOLE)
    # 洞外、环内仍命中
    assert point_in_fence(5.0, 1.0, fence)
    assert point_in_fence(1.0, 5.0, fence)
    # 洞内未命中
    assert not point_in_fence(5.0, 5.0, fence)
    assert not point_in_fence(4.5, 4.5, fence)
    # 洞边界未命中
    assert not point_in_fence(4.0, 5.0, fence)  # 洞南边
    assert not point_in_fence(5.0, 4.0, fence)  # 洞西边
    for lon, lat in [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]:
        assert not point_in_fence(lat, lon, fence)


def test_antimeridian_polygon_interior():
    for geom in (ANTIMERIDIAN, ANTIMERIDIAN_REV):
        fence = build_fence("am", 0, geom)
        # 对跖经线 180/-180 及两侧内部
        assert point_in_fence(0.5, 180.0, fence)
        assert point_in_fence(0.5, -180.0, fence)
        assert point_in_fence(0.5, 179.95, fence)
        assert point_in_fence(0.5, -179.95, fence)
        # 远离经线的点未命中
        assert not point_in_fence(0.5, 0.0, fence)
        assert not point_in_fence(0.5, 179.0, fence)
        assert not point_in_fence(0.5, -179.0, fence)
        assert not point_in_fence(2.0, 180.0, fence)
        assert not point_in_fence(-0.5, 180.0, fence)


def test_antimeridian_polygon_boundary():
    fence = build_fence("am", 0, ANTIMERIDIAN)
    # 外边界（含沿经线方向的竖边与横跨经线的横边）命中
    assert point_in_fence(0.0, 180.0, fence)
    assert point_in_fence(1.0, 180.0, fence)
    assert point_in_fence(0.5, 179.9, fence)
    assert point_in_fence(0.5, -179.9, fence)

    fence_rev = build_fence("amr", 0, ANTIMERIDIAN_REV)
    assert point_in_fence(0.0, 180.0, fence_rev)
    assert point_in_fence(0.5, -179.9, fence_rev)
    assert point_in_fence(0.5, 179.9, fence_rev)


def test_non_antimeridian_wide_boxes_unchanged():
    # 不跨经线的常规东 / 西经方框不应受连续化影响
    east = build_fence(
        "e",
        0,
        {"type": "Polygon", "coordinates": [
            [[170.0, 0.0], [175.0, 0.0], [175.0, 5.0], [170.0, 5.0], [170.0, 0.0]]
        ]},
    )
    assert point_in_fence(2.0, 172.5, east)
    assert not point_in_fence(2.0, -172.5, east)
    assert not point_in_fence(2.0, 180.0, east)

    west = build_fence(
        "w",
        0,
        {"type": "Polygon", "coordinates": [
            [[-175.0, 0.0], [-170.0, 0.0], [-170.0, 5.0], [-175.0, 5.0], [-175.0, 0.0]]
        ]},
    )
    assert point_in_fence(2.0, -172.5, west)
    assert not point_in_fence(2.0, 172.5, west)


def test_multipolygon_mixed_frames():
    fence = build_fence("m", 0, MULTI)
    # 第一块（普通区域）
    assert point_in_fence(0.5, 0.5, fence)
    # 第二块（跨 180°）
    assert point_in_fence(0.5, 180.0, fence)
    assert point_in_fence(0.5, -179.95, fence)
    # 两块之外
    assert not point_in_fence(5.0, 5.0, fence)
    assert not point_in_fence(0.5, 170.0, fence)


def test_hole_aligned_to_outer_frame_across_antimeridian():
    # 跨经线外环（展开为 179.9..180.1，lat 0..2）；
    # 洞以另一经度参考系编码（首点 -179.95，展开为 -180.05..-179.95），
    # 构建时需整体平移 +360 对齐外环
    geom = {
        "type": "Polygon",
        "coordinates": [
            [[179.9, 0.0], [-179.9, 0.0], [-179.9, 2.0], [179.9, 2.0], [179.9, 0.0]],
            [[-179.95, 0.9], [179.95, 0.9], [179.95, 1.1], [-179.95, 1.1], [-179.95, 0.9]],
        ],
    }
    fence = build_fence("amh", 0, geom)
    # 洞外命中
    assert point_in_fence(0.2, 180.0, fence)
    assert point_in_fence(1.8, 180.0, fence)
    assert point_in_fence(1.0, 179.92, fence)
    # 洞内与洞边界未命中
    assert not point_in_fence(1.0, 180.0, fence)
    assert not point_in_fence(1.0, -180.0, fence)
    assert not point_in_fence(0.9, 180.0, fence)


def test_assign_zones_priority_then_zone_id_tiebreak():
    segments = [seg(5.0, 5.0)]

    def square_fence(zone_id, priority):
        return build_fence(zone_id, priority, SQUARE)

    # 高优先级胜出，与输入顺序无关
    fences_a = [square_fence("zone-a", 1), square_fence("zone-high", 10)]
    fences_b = list(reversed(fences_a))
    assert assign_zones(segments, fences_a) == ["zone-high"]
    assert assign_zones(segments, fences_b) == ["zone-high"]

    # 同优先级按 zone_id 字典序决胜，与输入顺序无关
    fences_c = [square_fence("zone-b", 5), square_fence("zone-a", 5)]
    fences_d = list(reversed(fences_c))
    assert assign_zones(segments, fences_c) == ["zone-a"]
    assert assign_zones(segments, fences_d) == ["zone-a"]


def test_assign_zones_miss_is_none():
    fence = build_fence("z", 0, SQUARE)
    segments = [seg(5.0, 5.0), seg(50.0, 50.0)]
    assert assign_zones(segments, [fence]) == ["z", None]
    assert assign_zones(segments, []) == [None, None]


def test_summarize_fences_retains_zero_visits_in_request_order():
    z1 = build_fence("zone-b", 5, SQUARE)
    z2 = build_fence("zone-a", 5, SQUARE)
    # 同优先级按字典序：中心点 (5,5) 归 zone-a
    segments = [
        seg(5.0, 5.0, duration_s=100.0, start_s=0),
        seg(5.0, 5.0, duration_s=300.0, start_s=1000),
        seg(50.0, 50.0, duration_s=200.0, start_s=2000),
    ]
    assignments = assign_zones(segments, [z1, z2])
    assert assignments == ["zone-a", "zone-a", None]

    totals = summarize_fences(segments, assignments, [z1, z2])
    assert [t.zone_id for t in totals] == ["zone-b", "zone-a"]
    by_id = {t.zone_id: t for t in totals}
    assert by_id["zone-b"].visit_count == 0
    assert by_id["zone-b"].total_duration_s == 0.0
    assert by_id["zone-b"].stay_ids == []
    assert by_id["zone-a"].visit_count == 2
    assert by_id["zone-a"].total_duration_s == pytest.approx(400.0)
    assert by_id["zone-a"].stay_ids == [1, 2]
