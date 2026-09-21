"""跨时段热点归并核心算法（纯函数，无 Web 依赖）。

在停留检测（见 ``staypoint.detect_stays``）的结果之上做二次聚合：

1. 以每段停留的中心点为节点，两中心间的 Haversine 距离 <= hotspot_radius_m
   时连一条**无向边**；
2. 用并查集求无向图的**连通分量**——支持链式传递：甲邻乙、乙邻丙时，
   即使甲不邻丙，三者也同属一个分量；
3. 成员停留数（访问次数）>= min_visits 的分量判为**热点**，其余分量中的
   停留全部标记为**噪声**。

连通分量是无向图的固有划分，与边的枚举 / 合并顺序无关；热点编号按分量内
最早访问时间（最早 ``start_time``，持平再按最小停留下标）升序稳定分配。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .staypoint import StaySegment, haversine_m


@dataclass
class HotspotCluster:
    """一个热点（达标的连通分量）。"""

    hotspot_id: int
    member_indices: List[int]
    visit_count: int
    total_duration_s: float
    center_lat: float
    center_lon: float
    first_visit_time: datetime
    last_visit_time: datetime


def _duration_weighted_center(
    segments: Sequence[StaySegment], indices: Sequence[int]
) -> Tuple[float, float]:
    """按停留时长加权的中心；总权重为 0（各停留时长均为 0）时退化为等权均值。"""
    total_weight = sum(segments[i].duration_s for i in indices)
    if total_weight > 0:
        center_lat = (
            sum(segments[i].duration_s * segments[i].center_lat for i in indices)
            / total_weight
        )
        center_lon = (
            sum(segments[i].duration_s * segments[i].center_lon for i in indices)
            / total_weight
        )
    else:
        count = len(indices)
        center_lat = sum(segments[i].center_lat for i in indices) / count
        center_lon = sum(segments[i].center_lon for i in indices) / count
    return center_lat, center_lon


def find_hotspots(
    segments: Sequence[StaySegment],
    hotspot_radius_m: float,
    min_visits: int,
) -> Tuple[List[HotspotCluster], Dict[int, Optional[int]]]:
    """对停留区段做连通分量归并并判定热点。

    :param segments: 停留检测输出的停留区段（下标 0 起）
    :param hotspot_radius_m: 热点半径（米）：停留中心间距不超过该值则连边
    :param min_visits: 连通分量访问次数达到该值才判为热点
    :return: (按最早访问时间编号的热点列表, 停留下标 -> 热点编号 / None 噪声)
    """
    n = len(segments)
    assignment: Dict[int, Optional[int]] = {i: None for i in range(n)}
    if n == 0:
        return [], assignment

    parent = list(range(n))
    size = [1] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # 路径压缩
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # 按大小合并（仅影响树高；分量划分与合并顺序无关）
        if size[ra] < size[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        size[ra] += size[rb]

    # 边按 (i, j)（i < j）的规范顺序枚举；无向连通分量不随枚举顺序改变
    for i in range(n):
        si = segments[i]
        for j in range(i + 1, n):
            sj = segments[j]
            if (
                haversine_m(
                    si.center_lat, si.center_lon, sj.center_lat, sj.center_lon
                )
                <= hotspot_radius_m
            ):
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    qualifying = [members for members in groups.values() if len(members) >= min_visits]
    # 稳定编号：分量内最早访问时间升序，持平按最小停留下标
    qualifying.sort(
        key=lambda members: (
            min(segments[i].start_time for i in members),
            min(members),
        )
    )

    hotspots: List[HotspotCluster] = []
    for hotspot_id, members in enumerate(qualifying, start=1):
        members = sorted(members)
        total_duration_s = sum(segments[i].duration_s for i in members)
        center_lat, center_lon = _duration_weighted_center(segments, members)
        hotspots.append(
            HotspotCluster(
                hotspot_id=hotspot_id,
                member_indices=members,
                visit_count=len(members),
                total_duration_s=total_duration_s,
                center_lat=center_lat,
                center_lon=center_lon,
                first_visit_time=min(segments[i].start_time for i in members),
                last_visit_time=max(segments[i].end_time for i in members),
            )
        )
        for i in members:
            assignment[i] = hotspot_id

    return hotspots, assignment
