"""轨迹按指定围栏顺序依次访问的核对算法（纯函数，无 Web 依赖）。

在停留检测与围栏点面归属（见 ``staypoint.detect_stays`` /
``geofence.assign_zones``）之上，把停留序列压缩为**围栏访问事件流**，再与
``ordered_zone_ids`` 给定的期望路线做有序消费：

- **跳过未命中**：中心点未命中任何围栏的停留（``zone_id is None``）不产生事件，
  也不打断同一围栏的连续性；
- **连续停留合并**：同一围栏的连续停留只算一次访问事件（``stay_ids`` 保留全部
  命中停留，``entry_time`` / ``exit_time`` 取首入 / 末离时间）；
- **有序消费**：事件围栏 == 当前期望围栏 → 该步骤 ``matched``，进入下一目标；
  事件围栏 != 当前期望围栏 → 记录 ``unexpected``，目标指针不动；
- **超时缺失**：设置 ``max_step_gap_s`` 后，以上一已匹配步骤的**进入时间**为锚点，
  下一目标须在 ``锚点 + max_step_gap_s`` 时刻（含等号）之前出现，否则该步骤记为
  ``missing``；缺失步骤的截止时刻按相同时限**链式推进**（每个路线步骤各占一个
  时间槽）。迟到的期望访问按 ``missing`` 结算且事件一并消费（不再另记
  ``unexpected``）；第一个目标没有前序步骤，不做时限约束；
- **收尾缺失**：轨迹结束仍未消费的目标步骤一律记为 ``missing``（无截止时刻）。

``ordered_zone_ids`` 允许重复：如 ``["A", "B", "A"]`` 要求 A、B、A 各被独立
消费一次，因此两次 A 访问之间必须发生过一次其他围栏的访问。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from .staypoint import StaySegment


@dataclass(frozen=True)
class VisitEvent:
    """一次围栏访问：同一围栏的连续停留（中间可夹未命中停留）合并而成。"""

    zone_id: str
    stay_ids: List[int]
    entry_time: datetime
    exit_time: datetime


def build_visit_events(
    segments: Sequence[StaySegment], assignments: Sequence[Optional[str]]
) -> List[VisitEvent]:
    """把停留归属序列压缩为围栏访问事件流。

    未命中围栏的停留直接跳过；相邻（跳过未命中停留后）归属同一围栏的停留合并为
    一个事件，停留编号全部保留，进入 / 离开时间取组内首 / 末停留的时间。
    """
    groups: List[tuple] = []  # (zone_id, [segment_index, ...])
    current_zone: Optional[str] = None
    current_indices: List[int] = []
    for i, zone_id in enumerate(assignments):
        if zone_id is None:
            # 未命中围栏的停留直接跳过，且不打断同一围栏的连续性
            continue
        if zone_id == current_zone:
            current_indices.append(i)
        else:
            if current_zone is not None:
                groups.append((current_zone, current_indices))
            current_zone = zone_id
            current_indices = [i]
    if current_zone is not None:
        groups.append((current_zone, current_indices))

    events: List[VisitEvent] = []
    for zone_id, indices in groups:
        first = segments[indices[0]]
        last = segments[indices[-1]]
        events.append(
            VisitEvent(
                zone_id=zone_id,
                stay_ids=[i + 1 for i in indices],
                entry_time=first.start_time,
                exit_time=last.end_time,
            )
        )
    return events


@dataclass(frozen=True)
class RouteStepResult:
    """单个路线步骤的核对结果（顺序与 ordered_zone_ids 一一对应，允许重复）。"""

    step_index: int
    zone_id: str
    matched: bool
    stay_ids: List[int]
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    gap_s: Optional[float]


@dataclass(frozen=True)
class RouteViolation:
    """一次违规：missing（目标缺失 / 超时）或 unexpected（非当前目标到访）。"""

    kind: str
    step_index: Optional[int]
    zone_id: str
    stay_ids: List[int]
    entry_time: Optional[datetime]
    expected_zone_id: Optional[str]
    deadline_time: Optional[datetime]


@dataclass(frozen=True)
class RouteCheckResult:
    """路线核对结果。"""

    steps: List[RouteStepResult]
    violations: List[RouteViolation]
    matched_stay_ids: List[int]

    @property
    def overall_pass(self) -> bool:
        return not self.violations


def check_route(
    events: Sequence[VisitEvent],
    ordered_zone_ids: Sequence[str],
    max_step_gap_s: Optional[float] = None,
) -> RouteCheckResult:
    """按期望围栏顺序消费访问事件流。

    :param events: ``build_visit_events`` 产出的、按时间排序的访问事件
    :param ordered_zone_ids: 期望依次访问的围栏编号，允许重复
    :param max_step_gap_s: 相邻路线步骤进入时间的最大间隔（秒）；None 不做时限核对
    """
    n = len(ordered_zone_ids)
    slots: List[Optional[RouteStepResult]] = [None] * n
    violations: List[RouteViolation] = []
    matched_stay_ids: List[int] = []

    target = 0  # 当前期望步骤下标（0 基）
    # deadline 链时钟：上一已匹配步骤的进入时间，每跨过一个超时缺失步骤则推进一个时限
    deadline_anchor: Optional[datetime] = None
    # 上一实际匹配步骤的进入时间（用于计算 matched 步骤的 gap_s）
    last_match_time: Optional[datetime] = None
    gap = timedelta(seconds=max_step_gap_s) if max_step_gap_s is not None else None

    def mark_missing(idx: int, deadline: Optional[datetime]) -> None:
        zone_id = ordered_zone_ids[idx]
        slots[idx] = RouteStepResult(
            step_index=idx + 1,
            zone_id=zone_id,
            matched=False,
            stay_ids=[],
            entry_time=None,
            exit_time=None,
            gap_s=None,
        )
        violations.append(
            RouteViolation(
                kind="missing",
                step_index=idx + 1,
                zone_id=zone_id,
                stay_ids=[],
                entry_time=None,
                expected_zone_id=None,
                deadline_time=deadline,
            )
        )

    for event in events:
        # 以本次访问进入时间结算已超时的期望步骤（链式推进截止时刻）
        expired_zones: set = set()
        if gap is not None and deadline_anchor is not None:
            while target < n and event.entry_time > deadline_anchor + gap:
                deadline_anchor = deadline_anchor + gap
                expired_zones.add(ordered_zone_ids[target])
                mark_missing(target, deadline_anchor)
                target += 1

        if target < n and event.zone_id == ordered_zone_ids[target]:
            # 准时（含恰好压在截止时刻）出现的期望目标：匹配
            gap_s = (
                round((event.entry_time - last_match_time).total_seconds(), 3)
                if last_match_time is not None
                else None
            )
            slots[target] = RouteStepResult(
                step_index=target + 1,
                zone_id=event.zone_id,
                matched=True,
                stay_ids=list(event.stay_ids),
                entry_time=event.entry_time,
                exit_time=event.exit_time,
                gap_s=gap_s,
            )
            matched_stay_ids.extend(event.stay_ids)
            deadline_anchor = event.entry_time
            last_match_time = event.entry_time
            target += 1
        elif event.zone_id in expired_zones:
            # 迟到的正是本轮刚被判定超时的某个目标：步骤已记 missing，
            # 事件一并消费，不再另记 unexpected
            continue
        else:
            # 非当前目标的到访（路线已全部结算时 expected_zone_id 为 None）
            violations.append(
                RouteViolation(
                    kind="unexpected",
                    step_index=None,
                    zone_id=event.zone_id,
                    stay_ids=list(event.stay_ids),
                    entry_time=event.entry_time,
                    expected_zone_id=(
                        ordered_zone_ids[target] if target < n else None
                    ),
                    deadline_time=None,
                )
            )

    # 轨迹结束仍未消费的步骤全部记为缺失（无截止时刻）
    for idx in range(target, n):
        mark_missing(idx, None)

    return RouteCheckResult(
        steps=[slot for slot in slots if slot is not None],
        violations=violations,
        matched_stay_ids=matched_stay_ids,
    )
