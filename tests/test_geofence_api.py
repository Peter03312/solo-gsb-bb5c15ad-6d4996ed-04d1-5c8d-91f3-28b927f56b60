"""/api/v1/geofence-visits 接口集成测试。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

HOME = (39.900000, 116.400000)
WORK = (39.990000, 116.400000)
CAFE = (39.950000, 116.400000)

PARK = (41.000000, 116.400000)  # 从不到访


def visit(lat, lon, start_s):
    """一次 180s 的停留：4 个点，间隔 60s，坐标小幅抖动。"""
    return [
        {
            "lat": lat + dlat,
            "lon": lon,
            "timestamp": (BASE + timedelta(seconds=start_s + i * 60)).isoformat(),
        }
        for i, dlat in enumerate([0.0, 0.00001, -0.00001, 0.0])
    ]


POINTS = (
    visit(*HOME, start_s=0)
    + visit(*WORK, start_s=3000)
    + visit(*CAFE, start_s=6000)
    + visit(*HOME, start_s=9000)
)


def square_geofence(lat, lon, half=0.002):
    """以 (lat, lon) 为中心、边长 2*half 度的轴对齐方框。"""
    return [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
        [lon - half, lat - half],
    ]


def geofence(zone_id, rings_or_polygons, priority=0, multi=False):
    if multi:
        coords = rings_or_polygons
    else:
        coords = rings_or_polygons  # 单个环列表 -> Polygon
    return {
        "zone_id": zone_id,
        "priority": priority,
        "geometry": {
            "type": "MultiPolygon" if multi else "Polygon",
            "coordinates": coords,
        },
    }


def build_payload(points, geofences, **overrides):
    payload = {
        "points": points,
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
        "max_gap_s": 600,
        "geofences": geofences,
    }
    payload.update(overrides)
    return payload


HOME_FENCE = geofence("zone-home", [square_geofence(*HOME)])
WORK_FENCE = geofence("zone-work", [square_geofence(*WORK)])
CAFE_FENCE = geofence("zone-cafe", [square_geofence(*CAFE)])
PARK_FENCE = geofence("zone-park", [square_geofence(*PARK)])
ALL_FENCES = [HOME_FENCE, WORK_FENCE, CAFE_FENCE, PARK_FENCE]


def test_geofence_visits_assignment_and_summary():
    resp = client.post(
        "/api/v1/geofence-visits", json=build_payload(POINTS, ALL_FENCES)
    )
    assert resp.status_code == 200
    body = resp.json()

    assert [s["zone_id"] for s in body["stays"]] == [
        "zone-home",
        "zone-work",
        "zone-cafe",
        "zone-home",
    ]
    assert [s["stay_id"] for s in body["stays"]] == [1, 2, 3, 4]
    assert all(s["duration_s"] == 180.0 for s in body["stays"])

    # 汇总按请求中的围栏顺序输出，零访问围栏保留 0 值
    summaries = {g["zone_id"]: g for g in body["geofences"]}
    assert list(summaries) == ["zone-home", "zone-work", "zone-cafe", "zone-park"]
    assert summaries["zone-home"]["visit_count"] == 2
    assert summaries["zone-home"]["total_duration_s"] == 360.0
    assert summaries["zone-home"]["stay_ids"] == [1, 4]
    assert summaries["zone-work"]["visit_count"] == 1
    assert summaries["zone-work"]["total_duration_s"] == 180.0
    assert summaries["zone-work"]["stay_ids"] == [2]
    assert summaries["zone-cafe"]["visit_count"] == 1
    assert summaries["zone-cafe"]["stay_ids"] == [3]
    assert summaries["zone-park"]["visit_count"] == 0
    assert summaries["zone-park"]["total_duration_s"] == 0.0
    assert summaries["zone-park"]["stay_ids"] == []

    stats = body["stats"]
    assert stats["stay_count"] == 4
    assert stats["total_stay_duration_s"] == 720.0


def test_priority_then_zone_id_tiebreak():
    # 大方框覆盖全部三处地点：低优先级 big-z，与 home 围栏重叠时 home 胜
    big = geofence(
        "big-z",
        [[
            [116.390000, 39.890000],
            [116.410000, 39.890000],
            [116.410000, 40.000000],
            [116.390000, 40.000000],
            [116.390000, 39.890000],
        ]],
        priority=1,
    )
    # 高优先级重叠围栏覆盖工作地点
    work_vip = geofence("zone-work-vip", [square_geofence(*WORK)], priority=10)
    # 同优先级（10）但字典序更小的重叠围栏
    work_a = geofence("aaa-work", [square_geofence(*WORK, half=0.003)], priority=10)

    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(POINTS, [big, HOME_FENCE, work_vip, work_a]),
    )
    assert resp.status_code == 200
    body = resp.json()
    # stay 1,4：home 优先级 0 < big 1，但 home 框命中……
    assert [s["zone_id"] for s in body["stays"]] == [
        "big-z",       # home(0) 低于 big-z(1) -> big-z
        "aaa-work",    # 两个 priority=10 重叠，字典序 aaa-work 胜
        "big-z",       # cafe 只有 big-z 命中
        "big-z",
    ]
    # 调换围栏输入顺序，结果不变
    resp2 = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(
            POINTS, list(reversed([big, HOME_FENCE, work_vip, work_a]))
        ),
    )
    assert [s["zone_id"] for s in resp2.json()["stays"]] == [
        "big-z",
        "aaa-work",
        "big-z",
        "big-z",
    ]


def test_outer_boundary_hit_hole_miss():
    # HOME 中心压在外环底边：外环边界视为命中
    boundary_fence = geofence(
        "boundary",
        [[
            [116.398000, 39.900000],
            [116.402000, 39.900000],
            [116.402000, 39.904000],
            [116.398000, 39.904000],
            [116.398000, 39.900000],
        ]],
    )
    # 洞内 / 洞边界未命中：WORK 中心位于洞内（40.0-0.01..40.0+0.01）
    holed = geofence(
        "holed",
        [
            [
                [116.390000, 39.980000],
                [116.410000, 39.980000],
                [116.410000, 40.000000],
                [116.390000, 40.000000],
                [116.390000, 39.980000],
            ],
            [
                [116.399900, 39.989900],
                [116.400100, 39.989900],
                [116.400100, 39.990100],
                [116.399900, 39.990100],
                [116.399900, 39.989900],
            ],
        ],
    )
    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(POINTS, [boundary_fence, holed]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["zone_id"] for s in body["stays"]] == [
        "boundary",  # HOME 压外环边界
        None,        # WORK 在洞内 -> 未命中
        None,        # CAFE 在 holed 框外
        "boundary",
    ]
    summaries = {g["zone_id"]: g for g in body["geofences"]}
    assert summaries["holed"]["visit_count"] == 0
    assert summaries["boundary"]["visit_count"] == 2


def test_antimeridian_multipolygon_fence():
    # 两次停留都在 180° 经线附近（纬度 0 与 5.5 附近），经度在 ±180 两侧跳号；
    # 簇内经度跨经线时中心点用圆均值（约 180°），避免朴素均值错位到 ~90E
    def am_visit(lat, start_s):
        lons = [179.99, 180.0, -179.99, 180.0]
        return [
            {
                "lat": lat,
                "lon": lon,
                "timestamp": (BASE + timedelta(seconds=start_s + i * 60)).isoformat(),
            }
            for i, lon in enumerate(lons)
        ]

    points = am_visit(0.5, 0) + am_visit(5.5, 3000)
    # 多边形跨 180° 经线（179.95..-179.95，lat 0..1）；用 MultiPolygon 再加一块普通区域
    am_polygon = [
        [
            [179.95, 0.0],
            [-179.95, 0.0],
            [-179.95, 1.0],
            [179.95, 1.0],
            [179.95, 0.0],
        ]
    ]
    normal_polygon = [
        [
            [116.398, 39.898],
            [116.402, 39.898],
            [116.402, 39.902],
            [116.398, 39.902],
            [116.398, 39.898],
        ]
    ]
    fence = geofence(
        "dateline", [am_polygon, normal_polygon], priority=0, multi=True
    )
    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(
            points, [fence], distance_threshold_m=3000, max_gap_s=600
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["stays"]) == 2
    # 第一次中心（lat≈0.5，lon≈180）命中跨经线块；第二次（lat 5.5）在框外
    assert body["stays"][0]["zone_id"] == "dateline"
    assert body["stays"][1]["zone_id"] is None
    assert body["geofences"][0]["visit_count"] == 1
    assert body["geofences"][0]["total_duration_s"] == 180.0


def test_empty_geofences_and_no_stays():
    # 无围栏：所有停留 zone_id=null，汇总为空
    resp = client.post(
        "/api/v1/geofence-visits", json=build_payload(POINTS, [])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["stays"]) == 4
    assert all(s["zone_id"] is None for s in body["stays"])
    assert body["geofences"] == []

    # 单点轨迹识别不出停留：围栏汇总保留零访问
    resp = client.post(
        "/api/v1/geofence-visits",
        json=build_payload(
            [{"lat": 39.9, "lon": 116.4, "timestamp": BASE.isoformat()}],
            ALL_FENCES,
            min_stay_duration_s=300,
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stays"] == []
    assert len(body["geofences"]) == 4
    assert all(g["visit_count"] == 0 for g in body["geofences"])
    assert body["stats"]["stay_count"] == 0


def test_validation_errors():
    # 缺少 geofences
    bad = {"points": visit(*HOME, start_s=0)}
    assert client.post("/api/v1/geofence-visits", json=bad).status_code == 422

    # 非法几何类型
    bad_geom = build_payload(
        POINTS,
        [{"zone_id": "z", "geometry": {"type": "Point", "coordinates": [1, 2]}}],
    )
    assert client.post("/api/v1/geofence-visits", json=bad_geom).status_code == 422

    # 环未闭合
    unclosed = build_payload(
        POINTS,
        [geofence("z", [[[0, 0], [1, 0], [1, 1], [0, 1]]])],
    )
    assert client.post("/api/v1/geofence-visits", json=unclosed).status_code == 422

    # 环点数不足
    too_short = build_payload(
        POINTS,
        [geofence("z", [[[0, 0], [1, 0], [0, 0]]])],
    )
    assert client.post("/api/v1/geofence-visits", json=too_short).status_code == 422

    # 经纬度越界
    out_of_range = build_payload(
        POINTS,
        [geofence("z", [[[0, 91], [1, 91], [1, 92], [0, 92], [0, 91]]])],
    )
    assert client.post(
        "/api/v1/geofence-visits", json=out_of_range
    ).status_code == 422

    # zone_id 重复
    dup = build_payload(POINTS, [HOME_FENCE, HOME_FENCE])
    assert client.post("/api/v1/geofence-visits", json=dup).status_code == 422

    # zone_id 为空
    empty_id = build_payload(
        POINTS,
        [geofence("", [square_geofence(*HOME)])],
    )
    assert client.post(
        "/api/v1/geofence-visits", json=empty_id
    ).status_code == 422

    # 沿用的停留参数校验仍然生效
    bad_stay = build_payload(
        [{"lat": 91, "lon": 0, "timestamp": 0}], ALL_FENCES
    )
    assert client.post(
        "/api/v1/geofence-visits", json=bad_stay
    ).status_code == 422
