"""围栏点面归属与访问汇总核心算法（纯函数，无 Web 依赖）。

在停留检测（见 ``staypoint.detect_stays``）的结果之上，以每段停留的**中心点**
做 GeoJSON Polygon / MultiPolygon 的点面归属：

- **外环边界视为命中**：点落在任意外环（含顶点与边）上即视为面内；
- **洞内及洞边界视为未命中**：点落入任一孔洞内部或孔洞边界即视为面外；
- **跨 180° 经线**：每条环先按相邻顶点做经度连续化（unwrap），查询点再分别以
  ``lon、lon±360`` 三个经度像参与判定，因此跨越对跖经线的多边形无需调用方预切割；
- 多个围栏同时命中时按 ``priority`` 降序决胜，持平再按 ``zone_id`` 字典序决胜，
  归属结果与围栏输入顺序无关（zone_id 在请求层保证唯一）。

约定：环坐标为 GeoJSON 顺序 ``[lon, lat]``，外环为首环、其余为洞；
输入环未闭合（首末点不一致）时自动补齐。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .staypoint import StaySegment

# 浮点“压在边上”判定容差（度）；约毫米级，仅用于吸收二进制舍入误差
_EPS = 1e-9

# 一个位置 [lon, lat]
Position = Tuple[float, float]
# 闭合的线性环
Ring = List[Position]


def _normalize_ring(raw: Sequence[Sequence[float]]) -> Ring:
    """转为 (lon, lat) 浮点环；首末点不一致时自动闭合。"""
    ring: Ring = [(float(pos[0]), float(pos[1])) for pos in raw]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _unwrap_ring(ring: Ring) -> Ring:
    """将环的经度连续化到首顶点所在的经度参考系。

    沿环逐个顶点推进，相邻顶点经度差超过 ±180° 时整体平移 360°，
    使跨越 180° 经线的边在展开坐标中成为普通线段。
    """
    unwrapped: Ring = [ring[0]]
    offset = 0.0
    prev_lon = ring[0][0]
    for lon, lat in ring[1:]:
        while lon + offset - prev_lon > 180.0:
            offset -= 360.0
        while lon + offset - prev_lon < -180.0:
            offset += 360.0
        prev_lon = lon + offset
        unwrapped.append((prev_lon, lat))
    return unwrapped


def _align_ring(ring: Ring, ref_lon: float) -> Ring:
    """把已连续化的洞环平移到外环参考系（与外环首顶点经度差最小）。"""
    k = round((ref_lon - ring[0][0]) / 360.0)
    if k == 0:
        return ring
    return [(lon + 360.0 * k, lat) for lon, lat in ring]


def _on_segment(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> bool:
    """点 (px, py) 是否压在线段 (x1, y1)-(x2, y2) 上（含端点，带容差）。"""
    scale = max(1.0, abs(x2 - x1), abs(y2 - y1))
    tol = _EPS * scale
    if not (
        min(x1, x2) - tol <= px <= max(x1, x2) + tol
        and min(y1, y2) - tol <= py <= max(y1, y2) + tol
    ):
        return False
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    length = math.hypot(x2 - x1, y2 - y1)
    return abs(cross) <= tol * max(1.0, length)


def _point_in_ring(px: float, py: float, ring: Sequence[Position]):
    """射线法判定。严格内部返回 True，严格外部返回 False，压边界返回 ``"boundary"``。"""
    inside = False
    x1, y1 = ring[0]
    for i in range(1, len(ring)):
        x2, y2 = ring[i]
        if _on_segment(px, py, x1, y1, x2, y2):
            return "boundary"
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (x2 - x1) * (py - y1) / (y2 - y1)
            if x_cross > px:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def _point_in_polygon(px: float, py: float, polygon: Sequence[Ring]) -> bool:
    """外环边界命中；洞内及洞边界一律未命中。"""
    outer_hit = _point_in_ring(px, py, polygon[0])
    if not outer_hit:
        return False
    for hole in polygon[1:]:
        if _point_in_ring(px, py, hole):
            # True=洞内，"boundary"=洞边界，两者都判未命中
            return False
    return True


@dataclass(frozen=True)
class Fence:
    """一个围栏：优先级 + 若干已连续化的多边形（首环外环，其余为洞）。"""

    zone_id: str
    priority: int
    polygons: Tuple[Tuple[Ring, ...], ...]


def build_fence(zone_id: str, priority: int, geometry: dict) -> Fence:
    """从 GeoJSON geometry dict（结构已在请求模型层校验）构建围栏。"""
    if geometry["type"] == "Polygon":
        raw_polygons = [geometry["coordinates"]]
    else:  # MultiPolygon
        raw_polygons = geometry["coordinates"]

    polygons: List[Tuple[Ring, ...]] = []
    for raw_polygon in raw_polygons:
        rings = [_unwrap_ring(_normalize_ring(r)) for r in raw_polygon]
        ref_lon = rings[0][0][0]
        # 洞环可能以另一经度参考系编码，统一平移到外环参考系
        rings = [rings[0]] + [_align_ring(r, ref_lon) for r in rings[1:]]
        polygons.append(tuple(rings))
    return Fence(
        zone_id=zone_id, priority=priority, polygons=tuple(polygons)
    )


def point_in_fence(lat: float, lon: float, fence: Fence) -> bool:
    """点是否命中围栏。查询点依次以 lon、lon±360 参与各多边形判定。"""
    for polygon in fence.polygons:
        for shift in (-360.0, 0.0, 360.0):
            if _point_in_polygon(lon + shift, lat, polygon):
                return True
    return False


def assign_zones(
    segments: Sequence[StaySegment], fences: Sequence[Fence]
) -> List[Optional[str]]:
    """为每段停留的中心点唯一归属围栏。

    决胜顺序：priority 降序 → zone_id 字典序升序；未命中为 None。
    归属结果只与围栏属性有关，与 ``fences`` 的输入顺序无关。
    """
    ranked = sorted(fences, key=lambda f: (-f.priority, f.zone_id))
    assignments: List[Optional[str]] = []
    for seg in segments:
        zone_id: Optional[str] = None
        for fence in ranked:
            if point_in_fence(seg.center_lat, seg.center_lon, fence):
                zone_id = fence.zone_id
                break
        assignments.append(zone_id)
    return assignments


@dataclass(frozen=True)
class FenceTotals:
    """单个围栏的访问汇总。"""

    zone_id: str
    priority: int
    visit_count: int
    total_duration_s: float
    stay_ids: List[int]


def summarize_fences(
    segments: Sequence[StaySegment],
    assignments: Sequence[Optional[str]],
    fences: Sequence[Fence],
) -> List[FenceTotals]:
    """按围栏输入顺序汇总访问次数与累计停留时长（零访问围栏保留 0 值）。"""
    members: Dict[str, List[int]] = {f.zone_id: [] for f in fences}
    for i, zone_id in enumerate(assignments):
        if zone_id is not None:
            members[zone_id].append(i)

    totals: List[FenceTotals] = []
    for fence in fences:
        indices = members[fence.zone_id]
        totals.append(
            FenceTotals(
                zone_id=fence.zone_id,
                priority=fence.priority,
                visit_count=len(indices),
                total_duration_s=round(
                    sum(segments[i].duration_s for i in indices), 3
                ),
                stay_ids=[i + 1 for i in indices],
            )
        )
    return totals
