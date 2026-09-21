"""停留点检测核心算法（纯函数，无 Web 依赖）。

算法：经典 stay-point 检测（Li et al. 2008 风格）的流式实现——
按时间顺序逐点扫描，维护当前候选停留簇及其锚点（簇首点）：

1. 新点与锚点的 Haversine 距离 <= distance_threshold_m，则并入当前簇；
2. 距离超阈值，则关闭当前簇：簇时间跨度 >= min_stay_duration_s 判定为停留，
   随后以新点为锚点开启新簇；
3. 相邻点采样间隔 > max_gap_s 视为采样间断：无论距离多近都强制关闭当前簇，
   间断期间不计入任何停留时长。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

# IUGG 地球平均半径（米）
EARTH_RADIUS_M = 6371008.8

# (lat, lon, timestamp)
Point = Tuple[float, float, datetime]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两经纬度点间的 Haversine 大圆距离（米）。"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(
        dlambda / 2.0
    ) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass
class StaySegment:
    """检测出的停留区段，下标为原始输入点序列中的闭区间。"""

    start_index: int
    end_index: int
    start_time: datetime
    end_time: datetime
    center_lat: float
    center_lon: float

    @property
    def duration_s(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    @property
    def point_count(self) -> int:
        return self.end_index - self.start_index + 1


def detect_stays(
    points: Sequence[Point],
    distance_threshold_m: float,
    min_stay_duration_s: float,
    max_gap_s: Optional[float] = None,
) -> List[StaySegment]:
    """流式检测停留区段。

    :param points: 按时间升序的 (lat, lon, timestamp) 序列
    :param distance_threshold_m: 距离阈值（米）
    :param min_stay_duration_s: 最短停留时长（秒）
    :param max_gap_s: 最大采样间隔（秒），None 表示不做间断切分
    """
    stays: List[StaySegment] = []
    n = len(points)
    if n == 0:
        return stays

    def close_cluster(start_idx: int, end_idx: int) -> None:
        """关闭 [start_idx, end_idx] 簇，满足时长则记录为停留。"""
        start_ts = points[start_idx][2]
        end_ts = points[end_idx][2]
        if (end_ts - start_ts).total_seconds() < min_stay_duration_s:
            return
        count = end_idx - start_idx + 1
        lats = [points[i][0] for i in range(start_idx, end_idx + 1)]
        lons = [points[i][1] for i in range(start_idx, end_idx + 1)]
        center_lat = sum(lats) / count
        if max(lons) - min(lons) > 180.0:
            # 簇内经度跨越 180° 经线（如 179.999 与 -179.999 同属一个停留）：
            # 用 sin/cos 圆均值，避免朴素均值得到远离实际位置的 ~0°
            sin_sum = sum(math.sin(math.radians(lon)) for lon in lons)
            cos_sum = sum(math.cos(math.radians(lon)) for lon in lons)
            center_lon = math.degrees(math.atan2(sin_sum, cos_sum))
        else:
            center_lon = sum(lons) / count
        stays.append(
            StaySegment(
                start_index=start_idx,
                end_index=end_idx,
                start_time=start_ts,
                end_time=end_ts,
                center_lat=center_lat,
                center_lon=center_lon,
            )
        )

    cluster_start = 0
    anchor_lat, anchor_lon = points[0][0], points[0][1]

    for i in range(1, n):
        lat, lon, ts = points[i]
        prev_ts = points[i - 1][2]

        gap = max_gap_s is not None and (ts - prev_ts).total_seconds() > max_gap_s
        far = haversine_m(anchor_lat, anchor_lon, lat, lon) > distance_threshold_m

        if gap or far:
            close_cluster(cluster_start, i - 1)
            cluster_start = i
            anchor_lat, anchor_lon = lat, lon

    close_cluster(cluster_start, n - 1)
    return stays


def total_distance_m(points: Sequence[Point]) -> float:
    """轨迹总里程：相邻点 Haversine 距离之和（米）。"""
    return sum(
        haversine_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        for i in range(1, len(points))
    )
