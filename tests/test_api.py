"""API 集成测试。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 与 test_staypoint.SCENARIO 相同的场景，混用 epoch 与 ISO 8601 时间戳
POINTS = [
    {"lat": 39.900000, "lon": 116.400000, "timestamp": "2026-09-21T08:16:40Z"},
    {"lat": 39.900020, "lon": 116.400010, "timestamp": "2026-09-21T08:17:40Z"},
    {"lat": 39.899980, "lon": 116.399990, "timestamp": "2026-09-21T08:18:40Z"},
    {"lat": 39.900010, "lon": 116.400020, "timestamp": "2026-09-21T08:19:40Z"},
    {"lat": 39.899990, "lon": 116.399980, "timestamp": "2026-09-21T08:20:40Z"},
    {"lat": 39.910000, "lon": 116.400000, "timestamp": "2026-09-21T08:21:40Z"},
    {"lat": 39.910020, "lon": 116.400010, "timestamp": "2026-09-21T08:22:40Z"},
    {"lat": 39.909980, "lon": 116.399990, "timestamp": "2026-09-21T08:23:40Z"},
    {"lat": 39.910010, "lon": 116.400020, "timestamp": "2026-09-21T08:50:00Z"},
    {"lat": 39.910000, "lon": 116.400000, "timestamp": "2026-09-21T08:51:00Z"},
    {"lat": 39.910020, "lon": 116.400010, "timestamp": "2026-09-21T08:52:00Z"},
    {"lat": 39.909990, "lon": 116.399990, "timestamp": "2026-09-21T08:53:00Z"},
]

PAYLOAD = {
    "points": POINTS,
    "distance_threshold_m": 100,
    "min_stay_duration_s": 180,
    "max_gap_s": 600,
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_analyze_stays():
    resp = client.post("/api/v1/stays", json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["stays"]) == 2
    first, second = body["stays"]

    assert first["stay_id"] == 1
    assert (first["start_index"], first["end_index"]) == (0, 4)
    assert first["duration_s"] == 240.0
    assert first["point_count"] == 5
    assert abs(first["center_lat"] - 39.9) < 1e-4
    assert first["start_time"] == "2026-09-21T08:16:40Z"
    assert first["end_time"] == "2026-09-21T08:20:40Z"

    assert second["stay_id"] == 2
    assert (second["start_index"], second["end_index"]) == (8, 11)
    assert second["duration_s"] == 180.0

    stats = body["stats"]
    assert stats["point_count"] == 12
    assert stats["stay_count"] == 2
    assert stats["total_stay_duration_s"] == 420.0
    assert 1100.0 < stats["total_distance_m"] < 1200.0
    assert stats["total_duration_s"] == 2180.0


def test_analyze_stays_epoch_timestamps_and_default_gap():
    # epoch 时间戳；不传 max_gap_s 时默认 4 * min_stay_duration_s = 720s，
    # 1580s 的间断仍会被切分
    payload = {
        "points": [
            {"lat": p["lat"], "lon": p["lon"], "timestamp": 1758432000 + i * 60}
            for i, p in enumerate(POINTS[:5])
        ]
        + [
            {"lat": p["lat"], "lon": p["lon"], "timestamp": 1758432000 + 300 + i * 60}
            for i, p in enumerate(POINTS[5:8])
        ]
        + [
            {"lat": p["lat"], "lon": p["lon"], "timestamp": 1758432000 + 2000 + i * 60}
            for i, p in enumerate(POINTS[8:])
        ],
        "distance_threshold_m": 100,
        "min_stay_duration_s": 180,
    }
    resp = client.post("/api/v1/stays", json=payload)
    assert resp.status_code == 200
    stays = resp.json()["stays"]
    assert len(stays) == 2
    assert stays[0]["duration_s"] == 240.0
    assert stays[1]["duration_s"] == 180.0


def test_geojson():
    resp = client.post("/api/v1/stays/geojson", json=PAYLOAD)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()

    assert body["type"] == "FeatureCollection"
    # 1 条轨迹 LineString + 2 个停留 Point
    assert len(body["features"]) == 3

    line = body["features"][0]
    assert line["geometry"]["type"] == "LineString"
    assert len(line["geometry"]["coordinates"]) == 12
    # GeoJSON 坐标顺序为 [lon, lat]
    assert line["geometry"]["coordinates"][0] == [116.4, 39.9]
    assert line["properties"]["kind"] == "trajectory"
    assert line["properties"]["total_distance_m"] > 0

    stay_features = body["features"][1:]
    assert all(f["geometry"]["type"] == "Point" for f in stay_features)
    assert stay_features[0]["properties"]["stay_id"] == 1
    assert stay_features[0]["properties"]["duration_s"] == 240.0
    assert stay_features[1]["properties"]["stay_id"] == 2


def test_validation_errors():
    # 纬度越界
    bad = {"points": [{"lat": 91, "lon": 0, "timestamp": 0}]}
    assert client.post("/api/v1/stays", json=bad).status_code == 422
    # 空轨迹
    assert client.post("/api/v1/stays", json={"points": []}).status_code == 422
    # 非法阈值
    bad2 = {"points": POINTS, "distance_threshold_m": -1}
    assert client.post("/api/v1/stays", json=bad2).status_code == 422
