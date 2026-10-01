"""/api/v1/geofence-route-check 接口集成测试。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

HOME = (39.900000, 116.400000)
WORK = (39.990000, 116.400000)
CAFE = (39.950000, 116.400000)
FAR = (40.500000, 116.400000)  # 不在任何围栏内


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


# 4 次停留：家(08:00) → 公司(08:50) → 咖啡馆(09:40) → 家(10:30)
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


def geofence(zone_id, lat, lon, priority=0):
    return {
        "zone_id": zone_id,
        "priority": priority,
        "geometry": {"type": "Polygon", "coordinates": [square_geofence(lat, lon)]},
    }


HOME_FENCE = geofence("zone-home", *HOME)
WORK_FENCE = geofence("zone-work", *WORK)
CAFE_FENCE = geofence("zone-cafe", *CAFE)
ALL_FENCES = [HOME_FENCE, WORK_FENCE, CAFE_FENCE]


def build_payload(points=POINTS, geofences=ALL_FENCES, ordered=None, **overrides):
    payload = {
        "points": points,
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
        "max_gap_s": 600,
        "geofences": geofences,
        "ordered_zone_ids": ordered if ordered is not None else ["zone-home"],
    }
    payload.update(overrides)
    return payload


def test_exact_route_passes():
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(
            ordered=["zone-home", "zone-work", "zone-cafe", "zone-home"]
        ),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is True
    assert body["matched_stay_ids"] == [1, 2, 3, 4]
    assert body["violations"] == []
    assert [s["status"] for s in body["route_steps"]] == ["matched"] * 4
    assert [s["zone_id"] for s in body["route_steps"]] == [
        "zone-home",
        "zone-work",
        "zone-cafe",
        "zone-home",
    ]
    assert [s["step_index"] for s in body["route_steps"]] == [1, 2, 3, 4]
    assert [s["stay_id"] for s in body["route_steps"]] == [1, 2, 3, 4]
    assert body["route_steps"][0]["enter_time"] == "2026-09-21T08:00:00Z"
    assert body["route_steps"][3]["enter_time"] == "2026-09-21T10:30:00Z"


def test_unexpected_mid_route_and_after_completion():
    # 期望 home -> cafe：work 为非当前目标（mid-route），结尾的 home 为路线完成后的到访
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(ordered=["zone-home", "zone-cafe"]),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is False
    assert body["matched_stay_ids"] == [1, 3]
    assert [s["status"] for s in body["route_steps"]] == ["matched", "matched"]

    violations = body["violations"]
    assert [v["type"] for v in violations] == ["unexpected", "unexpected"]
    assert violations[0]["zone_id"] == "zone-work"
    assert violations[0]["stay_id"] == 2
    assert violations[0]["expected_zone_id"] == "zone-cafe"
    assert violations[0]["enter_time"] == "2026-09-21T08:50:00Z"
    assert violations[1]["zone_id"] == "zone-home"
    assert violations[1]["stay_id"] == 4
    assert violations[1]["expected_zone_id"] is None  # 路线已完成


def test_missing_when_target_never_appears():
    # 期望 home -> work -> cafe -> home -> work：第二段 work 不存在
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(
            ordered=["zone-home", "zone-work", "zone-cafe", "zone-home", "zone-work"]
        ),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is False
    assert body["matched_stay_ids"] == [1, 2, 3, 4]
    assert [s["status"] for s in body["route_steps"]] == ["matched"] * 4 + ["missing"]
    missing_step = body["route_steps"][4]
    assert missing_step["zone_id"] == "zone-work"
    assert missing_step["stay_id"] is None
    assert missing_step["enter_time"] is None

    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["type"] == "missing"
    assert v["zone_id"] == "zone-work"
    assert v["step_index"] == 5
    assert v["deadline_time"] is None  # 未设 max_step_gap_s


def test_consecutive_same_zone_stays_count_once_and_unmatched_skipped():
    # 家停留两段（中间隔着采样间断与一次围栏外停留），折叠为一次到访
    points = (
        visit(*HOME, start_s=0)
        + visit(*FAR, start_s=1200)  # 围栏外停留：跳过
        + visit(*HOME, start_s=2400)
        + visit(*WORK, start_s=4800)
    )
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(points=points, ordered=["zone-home", "zone-work"]),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is True
    # 折叠取首段停留：home -> stay 1（而非 stay 3），work -> stay 4
    assert body["matched_stay_ids"] == [1, 4]
    assert body["violations"] == []


def test_max_step_gap_timeout_marks_missing():
    # gap=2400s：home@08:00 匹配后，work 须在 08:40 前出现，实际 08:50 -> missing
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(
            ordered=["zone-home", "zone-work"], max_step_gap_s=2400
        ),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["overall_pass"] is False
    assert body["matched_stay_ids"] == [1]
    assert [s["status"] for s in body["route_steps"]] == ["matched", "missing"]

    v = body["violations"][0]
    assert v["type"] == "missing"
    assert v["zone_id"] == "zone-work"
    assert v["step_index"] == 2
    assert v["deadline_time"] == "2026-09-21T08:40:00Z"
    # 超时后实际出现的 work / cafe / home 均为 unexpected（路线已耗尽）
    rest = body["violations"][1:]
    assert [x["type"] for x in rest] == ["unexpected"] * 3
    assert [x["zone_id"] for x in rest] == ["zone-work", "zone-cafe", "zone-home"]


def test_max_step_gap_deadline_cascades():
    # gap=2400s：work 超时后时限顺延，cafe 须在 09:20 前出现，实际 09:40 -> 也 missing
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(
            ordered=["zone-home", "zone-work", "zone-cafe"], max_step_gap_s=2400
        ),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert [s["status"] for s in body["route_steps"]] == [
        "matched",
        "missing",
        "missing",
    ]
    missings = [v for v in body["violations"] if v["type"] == "missing"]
    assert [v["zone_id"] for v in missings] == ["zone-work", "zone-cafe"]
    assert missings[0]["deadline_time"] == "2026-09-21T08:40:00Z"
    assert missings[1]["deadline_time"] == "2026-09-21T09:20:00Z"


def test_max_step_gap_boundary_inclusive():
    # work@08:50 距 home@08:00 恰好 3000s：含等号，判定为按时
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(
            ordered=["zone-home", "zone-work"], max_step_gap_s=3000
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["status"] for s in body["route_steps"]] == ["matched", "matched"]
    assert body["matched_stay_ids"] == [1, 2]


def test_empty_ordered_zone_ids():
    # 空路线：任何到访都是 unexpected
    resp = client.post(
        "/api/v1/geofence-route-check", json=build_payload(ordered=[])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_pass"] is False
    assert body["route_steps"] == []
    assert body["matched_stay_ids"] == []
    assert [v["type"] for v in body["violations"]] == ["unexpected"] * 4

    # 空路线 + 无停留轨迹：通过
    resp = client.post(
        "/api/v1/geofence-route-check",
        json=build_payload(
            points=[{"lat": 39.9, "lon": 116.4, "timestamp": BASE.isoformat()}],
            ordered=[],
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_pass"] is True
    assert body["violations"] == []


def test_validation_errors():
    # 缺少 ordered_zone_ids
    payload = build_payload()
    del payload["ordered_zone_ids"]
    assert client.post("/api/v1/geofence-route-check", json=payload).status_code == 422

    # ordered_zone_ids 引用未定义的围栏
    assert (
        client.post(
            "/api/v1/geofence-route-check",
            json=build_payload(ordered=["zone-home", "zone-unknown"]),
        ).status_code
        == 422
    )

    # ordered_zone_ids 含空字符串
    assert (
        client.post(
            "/api/v1/geofence-route-check", json=build_payload(ordered=[""])
        ).status_code
        == 422
    )

    # max_step_gap_s 必须为正
    assert (
        client.post(
            "/api/v1/geofence-route-check",
            json=build_payload(ordered=["zone-home"], max_step_gap_s=0),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/geofence-route-check",
            json=build_payload(ordered=["zone-home"], max_step_gap_s=-10),
        ).status_code
        == 422
    )

    # 沿用的围栏校验仍然生效：zone_id 重复
    assert (
        client.post(
            "/api/v1/geofence-route-check",
            json=build_payload(
                geofences=[HOME_FENCE, HOME_FENCE], ordered=["zone-home"]
            ),
        ).status_code
        == 422
    )

    # 沿用的停留参数校验仍然生效：纬度越界
    assert (
        client.post(
            "/api/v1/geofence-route-check",
            json=build_payload(
                points=[{"lat": 91, "lon": 0, "timestamp": 0}],
                ordered=["zone-home"],
            ),
        ).status_code
        == 422
    )
