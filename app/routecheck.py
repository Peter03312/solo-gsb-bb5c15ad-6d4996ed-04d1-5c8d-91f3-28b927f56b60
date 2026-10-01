"""路线顺序核对核心算法（纯函数，无 Web 依赖）。

在停留检测（见 ``staypoint.detect_stays``）与围栏归属（见 ``geofence.assign_zones``）
的结果之上，核对轨迹是否**按给定路线依次访问围栏**：

1. **事件流**：按停留进入时间顺序消费命中围栏的停留——未命中任何围栏的停留跳过；
   同一围栏的**连续**停留折叠为一次访问（取首段停留的 ``stay_id`` 与进入时间代表）；
2. **严格顺序匹配**：按 ``ordered_zone_ids`` 顺序逐个等待目标——
   - 事件命中当前目标 → ``matched``，目标指针前进，时限锚点更新为该停留进入时间；
   - 事件不是当前目标 → 记录 ``unexpected`` 违规，事件被丢弃（不参与后续匹配，
     也不影响时限锚点）；
   - 设了 ``max_step_gap_s`` 时，当前目标须在上一个匹配停留的进入时间
     （首个目标以轨迹首点时间为锚）起 ``max_step_gap_s`` 秒内出现（含等号）；
     超时记 ``missing``，且后续目标的时限以被跳过目标的时限为锚**顺延**；
3. 轨迹结束后仍未匹配的目标一律记 ``missing``；
   全部目标匹配且无任何违规（missing / unexpected）时 ``overall_pass`` 为 True。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from .staypoint import StaySegment


@dataclass(frozen=True)
class ZoneEvent:
    """一次围栏到访事件：连续同围栏停留折叠后的最小消费单元。"""

    zone_id: str
    stay_id: int  # 折叠段首段停留的编号（从 1 开始）
    enter_time: datetime  # 折叠段首段停留的进入时间


def build_zone_events(
    segments: Sequence[StaySegment], assignments: Sequence[Optional[str]]
) -> List[ZoneEvent]:
    """由停留区段与归属结果构建围栏到访事件流。

    按停留进入时间（即检测输出顺序）消费：未命中围栏（None）的停留跳过；
    同一围栏的连续命中折叠为一次事件，取首段停留代表。
    """
    events: List[ZoneEvent] = []
    for i, (seg, zone_id) in enumerate(zip(segments, assignments)):
        if zone_id is None:
            continue
        if events and events[-1].zone_id == zone_id:
            continue
        events.append(ZoneEvent(zone_id=zone_id, stay_id=i + 1, enter_time=seg.start_time))
    return events


@dataclass(frozen=True)
class RouteStepResult:
    """期望路线中一步的核对结果。"""

    step_index: int  # 从 1 开始
    zone_id: str
    status: str  # "matched" | "missing"
    stay_id: Optional[int]
    enter_time: Optional[datetime]


@dataclass(frozen=True)
class RouteViolationRecord:
    """一条路线违规记录。"""

    type: str  # "missing" | "unexpected"
    zone_id: str  # missing：未出现的期望围栏；unexpected：实际命中的围栏
    step_index: Optional[int] = None  # missing：缺失目标的步序号
    stay_id: Optional[int] = None  # unexpected：触发违规的停留编号
    enter_time: Optional[datetime] = None  # unexpected：触发停留的进入时间
    expected_zone_id: Optional[str] = None  # unexpected：当时期望的围栏（路线已完成为 None）
    deadline_time: Optional[datetime] = None  # missing：到访时限（未设 max_step_gap_s 为 None）


@dataclass(frozen=True)
class RouteCheckResult:
    """路线核对结果。"""

    steps: List[RouteStepResult]
    violations: List[RouteViolationRecord]
    matched_stay_ids: List[int]
    overall_pass: bool


def check_route(
    events: Sequence[ZoneEvent],
    targets: Sequence[str],
    max_step_gap_s: Optional[float],
    start_time: datetime,
) -> RouteCheckResult:
    """按期望顺序核对围栏到访事件流。

    :param events: ``build_zone_events`` 的输出（按进入时间升序）
    :param targets: 期望依次到访的 zone_id 序列（允许重复，可为空）
    :param max_step_gap_s: 相邻目标进入时间的最大间隔（秒）；None 表示不设时限
    :param start_time: 首个目标的时限锚点（轨迹首点时间）
    """
    steps: List[RouteStepResult] = []
    violations: List[RouteViolationRecord] = []
    matched_stay_ids: List[int] = []

    n = len(targets)
    i = 0  # 当前等待匹配的目标下标
    anchor = start_time  # 时限锚点：上一匹配停留的进入时间（首目标为轨迹首点时间）
    gap = timedelta(seconds=max_step_gap_s) if max_step_gap_s is not None else None

    def record_missing() -> None:
        """将当前目标记为 missing，时限锚点顺延一个间隔。"""
        nonlocal i, anchor
        deadline = anchor + gap if gap is not None else None
        steps.append(
            RouteStepResult(
                step_index=i + 1,
                zone_id=targets[i],
                status="missing",
                stay_id=None,
                enter_time=None,
            )
        )
        violations.append(
            RouteViolationRecord(
                type="missing",
                zone_id=targets[i],
                step_index=i + 1,
                deadline_time=deadline,
            )
        )
        if gap is not None:
            anchor = anchor + gap
        i += 1

    for ev in events:
        # 该事件到达前已超时的目标依次记 missing（时限按步顺延）
        while i < n and gap is not None and ev.enter_time > anchor + gap:
            record_missing()
        if i < n and ev.zone_id == targets[i]:
            steps.append(
                RouteStepResult(
                    step_index=i + 1,
                    zone_id=targets[i],
                    status="matched",
                    stay_id=ev.stay_id,
                    enter_time=ev.enter_time,
                )
            )
            matched_stay_ids.append(ev.stay_id)
            anchor = ev.enter_time
            i += 1
        else:
            violations.append(
                RouteViolationRecord(
                    type="unexpected",
                    zone_id=ev.zone_id,
                    stay_id=ev.stay_id,
                    enter_time=ev.enter_time,
                    expected_zone_id=targets[i] if i < n else None,
                )
            )

    # 轨迹结束仍未匹配的目标一律 missing
    while i < n:
        record_missing()

    return RouteCheckResult(
        steps=steps,
        violations=violations,
        matched_stay_ids=matched_stay_ids,
        overall_pass=not violations,
    )
