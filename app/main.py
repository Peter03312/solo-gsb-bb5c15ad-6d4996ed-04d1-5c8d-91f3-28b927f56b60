"""FastAPI 应用入口。"""
from __future__ import annotations

from typing import List

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .geofence import assign_zones, build_fence, summarize_fences
from .hotspot import find_hotspots
from .models import (
    GeofenceStay,
    GeofenceSummary,
    GeofenceVisitRequest,
    GeofenceVisitResponse,
    Hotspot,
    HotspotRequest,
    HotspotResponse,
    HotspotStay,
    Stay,
    StayRequest,
    StayResponse,
    Stats,
)
from .staypoint import Point, StaySegment, detect_stays, total_distance_m

app = FastAPI(
    title="轨迹停留分析 API",
    version="1.2.0",
    description=(
        "提交带时间戳的有序轨迹，按 Haversine 距离与最短停留时长阈值流式合并连续点，"
        "识别停留区段（含跨采样间断切分），返回停留时长、中心点、原始点范围，"
        "并提供 GeoJSON 结果、总里程统计、基于停留中心连通分量的跨时段热点归并，"
        "以及基于停留中心点面归属的围栏访问统计（支持孔洞与跨 180° 经线多边形）。"
    ),
)


def _to_points(req: StayRequest) -> List[Point]:
    return [(p.lat, p.lon, p.timestamp) for p in req.points]


def _detect_segments(req: StayRequest) -> List[StaySegment]:
    points = _to_points(req)
    max_gap_s = (
        req.max_gap_s if req.max_gap_s is not None else 4 * req.min_stay_duration_s
    )
    return detect_stays(
        points,
        distance_threshold_m=req.distance_threshold_m,
        min_stay_duration_s=req.min_stay_duration_s,
        max_gap_s=max_gap_s,
    )


def _build_stats(points: List[Point], stays: List[Stay]) -> Stats:
    total_duration_s = (
        (points[-1][2] - points[0][2]).total_seconds() if len(points) > 1 else 0.0
    )
    return Stats(
        point_count=len(points),
        total_distance_m=round(total_distance_m(points), 3),
        total_duration_s=total_duration_s,
        stay_count=len(stays),
        total_stay_duration_s=sum(s.duration_s for s in stays),
    )


def _segments_to_stays(segments: List[StaySegment]) -> List[Stay]:
    return [
        Stay(
            stay_id=i + 1,
            center_lat=seg.center_lat,
            center_lon=seg.center_lon,
            start_time=seg.start_time,
            end_time=seg.end_time,
            duration_s=seg.duration_s,
            start_index=seg.start_index,
            end_index=seg.end_index,
            point_count=seg.point_count,
        )
        for i, seg in enumerate(segments)
    ]


def _analyze(req: StayRequest) -> StayResponse:
    points = _to_points(req)
    stays = _segments_to_stays(_detect_segments(req))
    return StayResponse(stays=stays, stats=_build_stats(points, stays))


def _analyze_hotspots(req: HotspotRequest) -> HotspotResponse:
    points = _to_points(req)
    segments = _detect_segments(req)
    clusters, assignment = find_hotspots(
        segments,
        hotspot_radius_m=req.hotspot_radius_m,
        min_visits=req.min_visits,
    )

    stays = [
        HotspotStay(
            stay_id=i + 1,
            center_lat=seg.center_lat,
            center_lon=seg.center_lon,
            start_time=seg.start_time,
            end_time=seg.end_time,
            duration_s=seg.duration_s,
            start_index=seg.start_index,
            end_index=seg.end_index,
            point_count=seg.point_count,
            hotspot_id=assignment[i],
            noise=assignment[i] is None,
        )
        for i, seg in enumerate(segments)
    ]
    hotspots = [
        Hotspot(
            hotspot_id=c.hotspot_id,
            visit_count=c.visit_count,
            total_duration_s=round(c.total_duration_s, 3),
            center_lat=c.center_lat,
            center_lon=c.center_lon,
            first_visit_time=c.first_visit_time,
            last_visit_time=c.last_visit_time,
            stay_ids=[idx + 1 for idx in c.member_indices],
        )
        for c in clusters
    ]
    return HotspotResponse(
        stays=stays, hotspots=hotspots, stats=_build_stats(points, stays)
    )


def _analyze_geofence_visits(req: GeofenceVisitRequest) -> GeofenceVisitResponse:
    points = _to_points(req)
    segments = _detect_segments(req)
    fences = [
        build_fence(g.zone_id, g.priority, g.geometry.model_dump())
        for g in req.geofences
    ]

    assignments = assign_zones(segments, fences)
    stays = [
        GeofenceStay(
            stay_id=i + 1,
            center_lat=seg.center_lat,
            center_lon=seg.center_lon,
            start_time=seg.start_time,
            end_time=seg.end_time,
            duration_s=seg.duration_s,
            start_index=seg.start_index,
            end_index=seg.end_index,
            point_count=seg.point_count,
            zone_id=zone_id,
        )
        for i, (seg, zone_id) in enumerate(zip(segments, assignments))
    ]
    totals = summarize_fences(segments, assignments, fences)
    summaries = [
        GeofenceSummary(
            zone_id=t.zone_id,
            priority=t.priority,
            visit_count=t.visit_count,
            total_duration_s=t.total_duration_s,
            stay_ids=t.stay_ids,
        )
        for t in totals
    ]
    return GeofenceVisitResponse(
        stays=stays, geofences=summaries, stats=_build_stats(points, stays)
    )


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/stays",
    response_model=StayResponse,
    summary="停留分析（JSON 结果 + 统计）",
)
def analyze_stays(req: StayRequest) -> StayResponse:
    return _analyze(req)


@app.post(
    "/api/v1/hotspots",
    response_model=HotspotResponse,
    summary="跨时段热点归并（停留中心连通分量）",
)
def analyze_hotspots(req: HotspotRequest) -> HotspotResponse:
    return _analyze_hotspots(req)


@app.post(
    "/api/v1/geofence-visits",
    response_model=GeofenceVisitResponse,
    summary="围栏访问统计（停留中心点面归属）",
)
def analyze_geofence_visits(req: GeofenceVisitRequest) -> GeofenceVisitResponse:
    return _analyze_geofence_visits(req)


@app.post(
    "/api/v1/stays/geojson",
    summary="停留分析（GeoJSON FeatureCollection）",
)
def analyze_stays_geojson(req: StayRequest) -> JSONResponse:
    result = _analyze(req)

    features: List[dict] = [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[p.lon, p.lat] for p in req.points],
            },
            "properties": {
                "kind": "trajectory",
                "point_count": result.stats.point_count,
                "total_distance_m": result.stats.total_distance_m,
                "total_duration_s": result.stats.total_duration_s,
            },
        }
    ]
    for s in result.stays:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [s.center_lon, s.center_lat],
                },
                "properties": {
                    "kind": "stay",
                    "stay_id": s.stay_id,
                    "start_time": s.start_time.isoformat(),
                    "end_time": s.end_time.isoformat(),
                    "duration_s": s.duration_s,
                    "start_index": s.start_index,
                    "end_index": s.end_index,
                    "point_count": s.point_count,
                },
            }
        )

    return JSONResponse(
        content={"type": "FeatureCollection", "features": features},
        media_type="application/geo+json",
    )
