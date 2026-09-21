"""/api/v1/hotspots 接口集成测试。"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

HOME = (39.900000, 116.400000)
MID = (39.901000, 116.400000)  # 距 HOME 约 111m，距 FAR 约 111m
FAR = (39.902000, 116.400000)  # 距 HOME 约 222m > 150m，但经 MID 链式可达
WORK = (39.990000, 116.400000)  # 距 HOME 约 10km，独立分量


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


def build_payload(points, **overrides):
    payload = {
        "points": points,
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
        "max_gap_s": 600,
        "hotspot_radius_m": 150,
        "min_visits": 2,
    }
    payload.update(overrides)
    return payload


POINTS = (
    visit(*HOME, start_s=0)
    + visit(*FAR, start_s=3000)
    + visit(*WORK, start_s=6000)
    + visit(*MID, start_s=9000)
    + visit(*HOME, start_s=12000)
    + visit(*WORK, start_s=15000)
)


def test_hotspots_chain_transitivity():
    # HOME-MID-FAR 两两相邻但 HOME 不邻 FAR，仍归为同一热点
    resp = client.post("/api/v1/hotspots", json=build_payload(POINTS))
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["stays"]) == 6
    stay_hotspot_ids = [s["hotspot_id"] for s in body["stays"]]
    # 顺序：HOME, FAR, WORK, MID, HOME, WORK
    assert stay_hotspot_ids == [1, 1, 2, 1, 1, 2]
    assert [s["noise"] for s in body["stays"]] == [False] * 6
    assert [s["stay_id"] for s in body["stays"]] == [1, 2, 3, 4, 5, 6]

    hotspots = body["hotspots"]
    assert len(hotspots) == 2

    home_hs, work_hs = hotspots
    assert home_hs["hotspot_id"] == 1
    assert home_hs["visit_count"] == 4
    assert home_hs["stay_ids"] == [1, 2, 4, 5]
    assert home_hs["total_duration_s"] == 4 * 180.0
    assert home_hs["first_visit_time"] == BASE.strftime("%Y-%m-%dT%H:%M:%SZ")
    # 加权中心：4 次停留等时长，等价于等权均值
    assert abs(home_hs["center_lat"] - sum([HOME[0], FAR[0], MID[0], HOME[0]]) / 4) < 1e-8
    assert home_hs["center_lon"] == 116.4

    assert work_hs["hotspot_id"] == 2
    assert work_hs["visit_count"] == 2
    assert work_hs["stay_ids"] == [3, 6]
    assert work_hs["total_duration_s"] == 360.0

    stats = body["stats"]
    assert stats["stay_count"] == 6
    assert stats["total_stay_duration_s"] == 6 * 180.0


def test_hotspots_noise_when_below_min_visits():
    # min_visits=5：两个分量都不达标，全部标记为噪声
    resp = client.post(
        "/api/v1/hotspots", json=build_payload(POINTS, min_visits=5)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hotspots"] == []
    assert all(s["hotspot_id"] is None for s in body["stays"])
    assert all(s["noise"] for s in body["stays"])


def test_hotspots_large_radius_merges_all():
    # 半径放大到覆盖 HOME->WORK 约 10km，全部停留并入单一热点
    resp = client.post(
        "/api/v1/hotspots", json=build_payload(POINTS, hotspot_radius_m=20000)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["hotspots"]) == 1
    hs = body["hotspots"][0]
    assert hs["hotspot_id"] == 1
    assert hs["visit_count"] == 6
    assert hs["stay_ids"] == [1, 2, 3, 4, 5, 6]
    assert hs["total_duration_s"] == 6 * 180.0
    assert all(s["hotspot_id"] == 1 for s in body["stays"])
    assert not any(s["noise"] for s in body["stays"])


def test_hotspots_empty_stays_all_noise():
    # 单点轨迹识别不出停留：无热点、无噪声停留，stats.stay_count=0
    payload = build_payload(
        [{"lat": 39.9, "lon": 116.4, "timestamp": BASE.isoformat()}],
        min_stay_duration_s=300,
    )
    resp = client.post("/api/v1/hotspots", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stays"] == []
    assert body["hotspots"] == []
    assert body["stats"]["stay_count"] == 0


def test_hotspots_validation_errors():
    # 缺少 hotspot_radius_m / min_visits
    bad = {
        "points": visit(*HOME, start_s=0),
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
    }
    assert client.post("/api/v1/hotspots", json=bad).status_code == 422
    # 非法半径与非法 min_visits
    assert (
        client.post(
            "/api/v1/hotspots",
            json=build_payload(POINTS, hotspot_radius_m=0),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/hotspots",
            json=build_payload(POINTS, min_visits=0),
        ).status_code
        == 422
    )
    # 沿用的停留参数校验仍然生效
    assert (
        client.post(
            "/api/v1/hotspots",
            json=build_payload([{"lat": 91, "lon": 0, "timestamp": 0}]),
        ).status_code
        == 422
    )
