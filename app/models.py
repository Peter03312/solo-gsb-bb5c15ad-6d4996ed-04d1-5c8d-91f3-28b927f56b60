"""Pydantic 请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class TrackPoint(BaseModel):
    """轨迹点：经纬度 + 时间戳。

    timestamp 支持三种形式：
    - ISO 8601 字符串，如 "2026-09-21T08:00:00Z"、"2026-09-21T08:00:00+08:00"
    - Unix 时间戳（秒，int/float），如 1758432000
    - 不带时区的 ISO 字符串按 UTC 处理
    """

    lat: float = Field(..., ge=-90, le=90, description="纬度，[-90, 90]")
    lon: float = Field(..., ge=-180, le=180, description="经度，[-180, 180]")
    timestamp: datetime = Field(..., description="ISO 8601 字符串或 Unix 秒时间戳")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_timestamp(cls, v):
        if isinstance(v, bool):
            raise ValueError("非法时间戳")
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(v, str):
            s = v.strip()
            try:
                return datetime.fromtimestamp(float(s), tz=timezone.utc)
            except ValueError:
                pass
            if s.endswith(("Z", "z")):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return v


class StayRequest(BaseModel):
    """停留分析请求。"""

    points: List[TrackPoint] = Field(
        ..., min_length=1, description="按时间升序排列的轨迹点"
    )
    distance_threshold_m: float = Field(
        100.0, gt=0, description="停留距离阈值（米）：连续点距锚点不超过该值则合并为同一停留"
    )
    min_stay_duration_s: float = Field(
        300.0, ge=0, description="最短停留时长（秒）：区段时间跨度达到该值才判定为停留"
    )
    max_gap_s: Optional[float] = Field(
        None,
        gt=0,
        description="最大采样间隔（秒）：相邻点间隔超过该值视为采样间断，强制切分段落；"
        "为空时默认取 4 * min_stay_duration_s",
    )


class Stay(BaseModel):
    """一个停留区段。"""

    stay_id: int = Field(..., description="停留序号，从 1 开始")
    center_lat: float = Field(..., description="停留中心纬度（区段内点均值）")
    center_lon: float = Field(..., description="停留中心经度（区段内点均值）")
    start_time: datetime = Field(..., description="进入停留区的时间（区段首点时间）")
    end_time: datetime = Field(..., description="离开停留区的时间（区段末点时间）")
    duration_s: float = Field(..., description="停留时长（秒）")
    start_index: int = Field(..., description="区段首点在请求 points 中的下标（含）")
    end_index: int = Field(..., description="区段末点在请求 points 中的下标（含）")
    point_count: int = Field(..., description="区段包含的轨迹点数")


class Stats(BaseModel):
    """整体统计。"""

    point_count: int = Field(..., description="轨迹点总数")
    total_distance_m: float = Field(..., description="总里程（米），相邻点 Haversine 距离之和")
    total_duration_s: float = Field(..., description="轨迹总时长（秒），首末点时间差")
    stay_count: int = Field(..., description="识别出的停留次数")
    total_stay_duration_s: float = Field(..., description="停留总时长（秒）")


class HotspotRequest(StayRequest):
    """热点归并请求：沿用停留检测参数，追加热点判定参数。"""

    hotspot_radius_m: float = Field(
        ...,
        gt=0,
        description="热点半径（米）：两段停留中心间 Haversine 距离不超过该值则连边",
    )
    min_visits: int = Field(
        ...,
        ge=1,
        description="最少访问次数：连通分量内停留数达到该值才判为热点，否则为噪声",
    )


class HotspotStay(Stay):
    """带热点归属的停留区段。"""

    hotspot_id: Optional[int] = Field(
        ..., description="所属热点编号（从 1 开始）；噪声停留为 null"
    )
    noise: bool = Field(..., description="是否为噪声停留（不属于任何热点）")


class Hotspot(BaseModel):
    """一个热点：中心间距离连边后达标的连通分量。"""

    hotspot_id: int = Field(..., description="热点编号，按最早访问时间从 1 开始")
    visit_count: int = Field(..., description="访问次数（分量内停留段数）")
    total_duration_s: float = Field(..., description="成员停留累计时长（秒）")
    center_lat: float = Field(..., description="热点中心纬度（按停留时长加权平均）")
    center_lon: float = Field(..., description="热点中心经度（按停留时长加权平均）")
    first_visit_time: datetime = Field(..., description="最早访问时间（最早停留进入时间）")
    last_visit_time: datetime = Field(..., description="最近离开时间（最晚停留离开时间）")
    stay_ids: List[int] = Field(..., description="成员停留的 stay_id（按编号升序）")


class HotspotResponse(BaseModel):
    stays: List[HotspotStay]
    hotspots: List[Hotspot]
    stats: Stats


class StayResponse(BaseModel):
    stays: List[Stay]
    stats: Stats


# --- 围栏访问（POST /api/v1/geofence-visits） ---


class GeoJSONGeometry(BaseModel):
    """GeoJSON Polygon / MultiPolygon 几何（坐标顺序 [lon, lat]）。

    跨 180° 经线的多边形按 GeoJSON 惯例在 ±180° 附近折返编码即可
    （如 [..., 179.9, ..., -179.9, ...]），服务内部自动连续化处理，
    无需调用方预先切割；单条环的经度跨度不得超过 180°。
    """

    type: Literal["Polygon", "MultiPolygon"]
    coordinates: List[Any] = Field(..., description="GeoJSON 坐标：Polygon 为环数组，MultiPolygon 为多边形数组")

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinates(cls, v, info):
        geom_type = info.data.get("type")
        if geom_type is None:
            return v

        def reject(msg: str):
            raise ValueError(msg)

        def check_position(pos) -> None:
            if (
                not isinstance(pos, (list, tuple))
                or len(pos) < 2
                or isinstance(pos[0], bool)
                or isinstance(pos[1], bool)
            ):
                reject("坐标点必须是至少包含 [lon, lat] 两个数值的数组")
            lon, lat = pos[0], pos[1]
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                reject("坐标点的经纬度必须为数值")
            if not -180.0 <= float(lon) <= 180.0:
                reject("经度必须在 [-180, 180] 范围内")
            if not -90.0 <= float(lat) <= 90.0:
                reject("纬度必须在 [-90, 90] 范围内")

        def check_ring(ring) -> None:
            if not isinstance(ring, list) or len(ring) < 4:
                reject("线性环至少需要 4 个坐标点（首尾相同）")
            for pos in ring:
                check_position(pos)
            if list(ring[0])[:2] != list(ring[-1])[:2]:
                reject("线性环必须闭合：首末坐标点需相同（未闭合时请手动补齐）")

        def check_polygon(polygon) -> None:
            if not isinstance(polygon, list) or len(polygon) < 1:
                reject("Polygon 必须至少包含一个外环")
            for ring in polygon:
                check_ring(ring)

        if geom_type == "Polygon":
            check_polygon(v)
        else:
            if not isinstance(v, list):
                reject("MultiPolygon 的 coordinates 必须是数组")
            for polygon in v:
                check_polygon(polygon)
        return v


class Geofence(BaseModel):
    """一个电子围栏：编号、优先级与 Polygon / MultiPolygon 几何。"""

    zone_id: str = Field(..., min_length=1, description="围栏唯一编号")
    priority: int = Field(
        0,
        description="优先级：多围栏同时命中时取较大值，持平再按 zone_id 字典序决胜",
    )
    geometry: GeoJSONGeometry = Field(
        ..., description="GeoJSON Polygon 或 MultiPolygon（[lon, lat] 坐标）"
    )


class GeofenceVisitRequest(StayRequest):
    """围栏访问请求：沿用停留检测参数，追加围栏列表。"""

    geofences: List[Geofence] = Field(
        ..., description="围栏列表（zone_id 须唯一），可为空数组"
    )

    @model_validator(mode="after")
    def _check_unique_zone_ids(self):
        seen = set()
        for fence in self.geofences:
            if fence.zone_id in seen:
                raise ValueError(f"zone_id 重复：{fence.zone_id}")
            seen.add(fence.zone_id)
        return self


class GeofenceStay(Stay):
    """带围栏归属的停留区段。"""

    zone_id: Optional[str] = Field(
        ..., description="命中的围栏编号；未命中任何围栏为 null"
    )


class GeofenceSummary(BaseModel):
    """单个围栏的访问汇总（按请求中围栏顺序输出，零访问也保留）。"""

    zone_id: str = Field(..., description="围栏编号")
    priority: int = Field(..., description="围栏优先级")
    visit_count: int = Field(..., description="访问次数（中心点命中该围栏的停留段数）")
    total_duration_s: float = Field(..., description="命中停留的累计时长（秒）")
    stay_ids: List[int] = Field(
        ..., description="命中该围栏的停留 stay_id（按停留编号升序）"
    )


class GeofenceVisitResponse(BaseModel):
    stays: List[GeofenceStay]
    geofences: List[GeofenceSummary]
    stats: Stats


# --- 围栏路线核对（POST /api/v1/geofence-route-check） ---


class GeofenceRouteCheckRequest(GeofenceVisitRequest):
    """围栏路线核对请求：沿用停留检测参数与围栏列表，追加期望路线。"""

    ordered_zone_ids: List[str] = Field(
        ...,
        min_length=1,
        description="期望依次访问的围栏编号序列，允许重复（重复项需分别到访消费）",
    )
    max_step_gap_s: Optional[float] = Field(
        None,
        gt=0,
        description="相邻路线步骤进入时间的最大间隔（秒）：超过则缺失步骤记 missing；"
        "为空时不做时限核对",
    )

    @model_validator(mode="after")
    def _check_ordered_zones_known(self):
        known = {fence.zone_id for fence in self.geofences}
        unknown = [z for z in self.ordered_zone_ids if z not in known]
        if unknown:
            raise ValueError(
                f"ordered_zone_ids 中的 zone_id 必须存在于 geofences 中：{unknown}"
            )
        return self


class RouteStep(BaseModel):
    """单个路线步骤的核对结果（顺序与请求 ordered_zone_ids 一一对应）。"""

    step_index: int = Field(..., description="路线步骤序号，从 1 开始")
    zone_id: str = Field(..., description="该步骤期望访问的围栏编号")
    matched: bool = Field(..., description="是否按时按序命中")
    stay_ids: List[int] = Field(
        ...,
        description="匹配该步骤的访问事件所含停留 stay_id（同一围栏连续停留合并为一次）；"
        "未匹配为 []",
    )
    entry_time: Optional[datetime] = Field(
        ..., description="匹配访问的进入时间（首个命中停留的进入时间）；未匹配为 null"
    )
    exit_time: Optional[datetime] = Field(
        ..., description="匹配访问的离开时间（末个命中停留的离开时间）；未匹配为 null"
    )
    gap_s: Optional[float] = Field(
        ...,
        description="匹配进入时间相对上一已匹配步骤进入时间的间隔（秒）；"
        "第一步或未匹配为 null",
    )


class RouteViolation(BaseModel):
    """一次路线违规。"""

    type: Literal["missing", "unexpected"] = Field(
        ...,
        description="违规类型：missing=期望步骤缺失（未出现或超时），"
        "unexpected=出现非当前目标围栏的访问",
    )
    step_index: Optional[int] = Field(
        ...,
        description="missing 对应缺失的路线步骤序号；unexpected 不属于任何步骤，为 null",
    )
    zone_id: str = Field(..., description="缺失的围栏编号（missing）或实际到访围栏编号（unexpected）")
    stay_ids: List[int] = Field(
        ..., description="unexpected 访问事件所含停留 stay_id；missing 为 []"
    )
    entry_time: Optional[datetime] = Field(
        ..., description="unexpected 访问的进入时间；missing 为 null"
    )
    expected_zone_id: Optional[str] = Field(
        ...,
        description="unexpected 发生时当前期望的围栏编号（路线已走完则为 null）；missing 为 null",
    )
    deadline_time: Optional[datetime] = Field(
        ...,
        description="因超时缺失时的截止时刻（锚点 + max_step_gap_s，链式推进）；"
        "轨迹结束仍未出现或未设置时限时为 null",
    )


class GeofenceRouteCheckResponse(BaseModel):
    """围栏路线核对响应。"""

    matched_stay_ids: List[int] = Field(
        ...,
        description="所有匹配访问事件消费的停留 stay_id（按访问时间升序、已做连续合并去重）",
    )
    route_steps: List[RouteStep] = Field(
        ..., description="按顺序的路线步骤核对结果，长度与 ordered_zone_ids 一致（允许重复）"
    )
    violations: List[RouteViolation] = Field(
        ..., description="违规列表：missing / unexpected，按发生时间排序"
    )
    overall_pass: bool = Field(
        ..., description="是否完全按路线依次、按时访问（无任何违规即 true）"
    )
