"""/api/v1/geofence-route-check 接口集成测试。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

HOME = (39.900000, 116.400000)
WORK = (39.990000, 116.400000)
CAFE = (39.950000, 116.400000)
SHOP = (39.920000, 116.400000)


def visit(lat, lon, start_s, duration_s=180, step_s=60):
    """一次停留：duration_s 跨度，每 step_s 一个点，坐标 ±0.00001° 抖动。"""
    n_points = max(2, int(round(duration_s / step_s)) + 1)
    jitter = [0.0, 0.00001, -0.00001, 0.0]
    return [
        {
            "lat": lat + jitter[i % len(jitter)],
            "lon": lon,
            "timestamp": (
                BASE + timedelta(seconds=start_s + i * step_s)
            ).isoformat(),
        }
        for i in range(n_points)
    ]


def square_geofence(lat, lon, half=0.002):
    return [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
        [lon - half, lat - half],
    ]


def geofence(zone_id, lat, lon, priority=0):
    return {
        "zone_id": zone_id,
        "priority": priority,
        "geometry": {"type": "Polygon", "coordinates": [square_geofence(lat, lon)]},
    }


HOME_FENCE = geofence("zone-home", *HOME)
WORK_FENCE = geofence("zone-work", *WORK)
CAFE_FENCE = geofence("zone-cafe", *CAFE)
SHOP_FENCE = geofence("zone-shop", *SHOP)
ALL_FENCES = [HOME_FENCE, WORK_FENCE, CAFE_FENCE, SHOP_FENCE]


def build_payload(points, ordered_zone_ids, geofences=None, **overrides):
    payload = {
        "points": points,
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
        "max_gap_s": 600,
        "geofences": ALL_FENCES if geofences is None else geofences,
        "ordered_zone_ids": ordered_zone_ids,
    }
    payload.update(overrides)
    return payload


def test_route_check_happy_path():
    # home(0) -> work(1000) -> cafe(2000) -> home(3000)，相邻间隔 1000s
    points = (
        visit(*HOME, 0)
        + visit(*WORK, 1000)
        + visit(*CAFE, 2000)
        + visit(*HOME, 3000)
    )
    payload = build_payload(
        points,
        ["zone-home", "zone-work", "zone-cafe", "zone-home"],
        max_step_gap_s=1200,
    )
    resp = client.post("/api/v1/geofence-route-check", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is True
    assert body["violations"] == []
    assert body["matched_stay_ids"] == [1, 2, 3, 4]
    assert [s["zone_id"] for s in body["route_steps"]] == [
        "zone-home",
        "zone-work",
        "zone-cafe",
        "zone-home",
    ]
    assert all(s["matched"] for s in body["route_steps"])
    assert [s["step_index"] for s in body["route_steps"]] == [1, 2, 3, 4]
    assert [s["stay_ids"] for s in body["route_steps"]] == [
        [1], [2], [3], [4]
    ]
    # gap_s：第一步 null，其余为相邻匹配步骤进入时间差
    assert [s["gap_s"] for s in body["route_steps"]] == [
        None, 1000.0, 1000.0, 1000.0
    ]
    assert body["route_steps"][0]["entry_time"].endswith("08:00:00Z")
    assert body["route_steps"][-1]["exit_time"].endswith("08:53:00Z")


def test_route_check_unexpected_and_missing():
    # 期望 home -> work -> cafe；实际 home -> cafe（work 缺失）
    points = visit(*HOME, 0) + visit(*CAFE, 2000)
    payload = build_payload(points, ["zone-home", "zone-work", "zone-cafe"])
    resp = client.post("/api/v1/geofence-route-check", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is False
    assert body["matched_stay_ids"] == [1]
    kinds = [(v["type"], v["zone_id"]) for v in body["violations"]]
    # cafe 到访时当前目标为 work -> unexpected；收尾 work、cafe 依次 missing
    assert kinds[0] == ("unexpected", "zone-cafe")
    assert ("missing", "zone-work") in kinds
    assert ("missing", "zone-cafe") in kinds
    unexpected = body["violations"][0]
    assert unexpected["expected_zone_id"] == "zone-work"
    assert unexpected["stay_ids"] == [2]
    assert unexpected["entry_time"].endswith("08:33:20Z")
    assert unexpected["step_index"] is None
    missing_work = next(
        v for v in body["violations"]
        if v["type"] == "missing" and v["zone_id"] == "zone-work"
    )
    assert missing_work["step_index"] == 2
    assert missing_work["deadline_time"] is None  # 未设置时限
    by_zone = {s["zone_id"]: s for s in body["route_steps"]}
    assert by_zone["zone-home"]["matched"] is True
    assert by_zone["zone-work"]["matched"] is False
    assert by_zone["zone-cafe"]["matched"] is False


def test_route_check_timeout_missing_with_deadline():
    # home@0，work@2000；时限 1000 -> work 超时缺失，迟到事件静默消费
    points = visit(*HOME, 0) + visit(*WORK, 2000)
    payload = build_payload(
        points, ["zone-home", "zone-work"], max_step_gap_s=1000
    )
    resp = client.post("/api/v1/geofence-route-check", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is False
    assert body["matched_stay_ids"] == [1]
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["type"] == "missing"
    assert v["zone_id"] == "zone-work"
    assert v["deadline_time"] == "2026-09-21T08:16:40Z"

    # 放宽到 2000（恰好压线，含等号）-> 通过
    payload["max_step_gap_s"] = 2000
    body = client.post("/api/v1/geofence-route-check", json=payload).json()
    assert body["overall_pass"] is True
    assert body["route_steps"][1]["gap_s"] == 2000.0


def test_route_check_consecutive_same_zone_merged():
    # 两次 home 停留之间只夹一次未命中任何围栏的停留（shop 围栏不到访，
    # 中途点 41° 完全无命中），合并为一次 home 访问
    middle = visit(41.0, 116.4, 1000)
    points = visit(*HOME, 0) + middle + visit(*HOME, 2000)
    payload = build_payload(points, ["zone-home", "zone-work"])
    resp = client.post("/api/v1/geofence-route-check", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    steps = body["route_steps"]
    assert steps[0]["matched"] is True
    assert steps[0]["stay_ids"] == [1, 3]  # 连续 home 停留合并
    assert steps[0]["entry_time"].endswith("08:00:00Z")
    assert steps[0]["exit_time"].endswith("08:36:20Z")
    assert body["matched_stay_ids"] == [1, 3]
    assert steps[1]["matched"] is False  # work 从未出现
    assert any(
        v["type"] == "missing" and v["zone_id"] == "zone-work"
        for v in body["violations"]
    )


def test_route_check_repeated_zone_requires_revisit():
    # 路线 home -> work -> home；实际只 home -> work（最后一次 home 缺失）
    points = visit(*HOME, 0) + visit(*WORK, 1000)
    payload = build_payload(points, ["zone-home", "zone-work", "zone-home"])
    body = client.post("/api/v1/geofence-route-check", json=payload).json()
    assert body["overall_pass"] is False
    missing = [v for v in body["violations"] if v["type"] == "missing"]
    assert [(v["zone_id"], v["step_index"]) for v in missing] == [
        ("zone-home", 3)
    ]
    assert body["matched_stay_ids"] == [1, 2]


def test_route_check_no_stays_all_missing():
    payload = build_payload(
        [{"lat": 39.9, "lon": 116.4, "timestamp": BASE.isoformat()}],
        ["zone-home", "zone-work"],
        min_stay_duration_s=300,
    )
    body = client.post("/api/v1/geofence-route-check", json=payload).json()
    assert body["overall_pass"] is False
    assert body["matched_stay_ids"] == []
    assert [v["type"] for v in body["violations"]] == ["missing", "missing"]
    assert len(body["route_steps"]) == 2
    assert all(s["matched"] is False for s in body["route_steps"])


def test_route_check_validation_errors():
    base_points = visit(*HOME, 0)

    # 缺少 ordered_zone_ids
    bad = {
        "points": base_points,
        "geofences": ALL_FENCES,
        "min_stay_duration_s": 180,
    }
    assert client.post("/api/v1/geofence-route-check", json=bad).status_code == 422

    # ordered_zone_ids 为空
    bad = build_payload(base_points, [])
    assert client.post("/api/v1/geofence-route-check", json=bad).status_code == 422

    # ordered_zone_ids 引用了不存在的围栏
    bad = build_payload(base_points, ["zone-home", "zone-unknown"])
    assert client.post("/api/v1/geofence-route-check", json=bad).status_code == 422

    # max_step_gap_s 非正
    bad = build_payload(base_points, ["zone-home"], max_step_gap_s=0)
    assert client.post("/api/v1/geofence-route-check", json=bad).status_code == 422

    # geofences 内 zone_id 重复（沿用父模型校验）
    bad = build_payload(
        base_points, ["zone-home"], geofences=[HOME_FENCE, HOME_FENCE]
    )
    assert client.post("/api/v1/geofence-route-check", json=bad).status_code == 422

    # 空 geofences 但路线非空 -> zone_id 无法识别
    bad = build_payload(base_points, ["zone-home"], geofences=[])
    assert client.post("/api/v1/geofence-route-check", json=bad).status_code == 422
