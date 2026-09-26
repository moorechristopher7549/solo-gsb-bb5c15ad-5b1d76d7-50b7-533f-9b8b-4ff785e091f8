"""字幕阅读负载审查：按阅读速度把每块正文折算为所需阅读时长，
在同一阅读时间轴上按最早出点优先模拟分配，统计合格/逾期。

规则：

- 每块正文去掉全部空白后计显示宽度：东亚宽字符（East Asian Width
  为 W/F）计 2，其余字符计 1；所需阅读毫秒数 = ceil(宽度 × 1000 /
  阅读速度)，阅读速度为每秒可读的显示宽度单位数（4～30 的整数）。
- 每块只允许在自身 [入点, 出点) 内被阅读；入点不早于出点的块
  指出块号并拒绝审查。
- 所有块共用同一条阅读时间轴，同一时刻只读一块；按最早出点优先
  分配（出点相同按解析序位），在入点、出点或块读完时重新评估
  当前阅读的块——后到但出点更早的块会抢占当前块，被抢占块已读
  进度保留，之后可继续。
- 块到出点仍未读完即为逾期，记录其未分配毫秒数（剩余所需阅读时长）。
"""
from __future__ import annotations

import heapq
import unicodedata
from dataclasses import dataclass

from .parsers import SrtItem

MIN_SPEED = 4    # 阅读速度下限（显示宽度单位/秒）
MAX_SPEED = 30   # 阅读速度上限（显示宽度单位/秒）


@dataclass
class ReadRow:
    """单个字幕块的阅读负载审查结果。"""

    block_pos: int      # 块号：SRT 解析序位（从 1 开始）
    index: int          # SRT 块内声明的序号
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    text: str           # 正文（多行以空格连接，供预览）
    width: int          # 去空白后的显示宽度（单位数）
    required_ms: int    # 所需阅读时长（毫秒，向上取整）
    allocated_ms: int   # 时间轴上实际分到的阅读时长（毫秒）
    status: str         # "ok" 合格 / "overdue" 逾期

    @property
    def unallocated_ms(self) -> int:
        """未分配毫秒数（逾期块的剩余所需阅读时长）。"""
        return self.required_ms - self.allocated_ms


def display_width(text: str) -> int:
    """去空白后的显示宽度：东亚宽字符（W/F）计 2，其余字符计 1。"""
    width = 0
    for ch in text:
        if ch.isspace():
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def required_read_ms(width: int, speed: int) -> int:
    """所需阅读毫秒数：宽度按速度（单位/秒）换算为毫秒，向上取整。"""
    return -(-(width * 1000) // speed)


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def audit_reading_load(items: list[SrtItem], speed: int) -> list[ReadRow]:
    """在同一阅读时间轴上模拟分配，逐块给出所需/已分配时长与合格/逾期。

    入点不早于出点的块抛带具体块号的 ValueError（调用方应拒绝预览/导出）。
    返回行按时间（入点、再按解析序位）排序。
    """
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError(
            f"阅读速度须为 {MIN_SPEED}~{MAX_SPEED} 之间的整数"
        )
    n = len(items)
    widths: list[int] = []
    required: list[int] = []
    for pos, item in enumerate(items, start=1):
        if item.end_ms <= item.start_ms:
            label = _block_label(pos, item.index)
            raise ValueError(
                f"{label}入点不早于出点"
                f"（{item.start_tc} --> {item.end_tc}），"
                "无法在自身区间内安排阅读"
            )
        width = display_width(item.raw_text)
        widths.append(width)
        required.append(required_read_ms(width, speed))

    # 事件驱动模拟：同一时刻只读一块，最早出点优先（同出点按解析序位），
    # 在入点、出点或块读完时重新评估当前阅读的块。
    remaining = list(required)
    allocated = [0] * n
    arrivals = sorted(range(n), key=lambda i: (items[i].start_ms, i))
    heap: list[tuple[int, int]] = []   # (出点, 解析序位) 的可读块
    ptr = 0                            # 下一个未到达块在 arrivals 中的位置
    t = 0
    while ptr < n or heap:
        if not heap:
            t = items[arrivals[ptr]].start_ms
        while ptr < n and items[arrivals[ptr]].start_ms <= t:
            i = arrivals[ptr]
            heapq.heappush(heap, (items[i].end_ms, i))
            ptr += 1
        end_cur, cur = heap[0]
        # 下一事件时刻：当前块到出点 / 当前块读完 / 下一块到达
        event_t = min(end_cur, t + remaining[cur])
        if ptr < n and items[arrivals[ptr]].start_ms < event_t:
            event_t = items[arrivals[ptr]].start_ms
        dt = event_t - t
        remaining[cur] -= dt
        allocated[cur] += dt
        t = event_t
        if remaining[cur] == 0:
            heapq.heappop(heap)        # 当前块读完（恰在出点读完也算合格）
        # 出点到达仍未读完的块逾期：弹出后不再参与分配，
        # 其未分配毫秒数 = 剩余所需阅读时长（required - allocated）
        while heap and heap[0][0] <= t:
            heapq.heappop(heap)

    rows: list[ReadRow] = []
    for pos, item in enumerate(items, start=1):
        i = pos - 1
        rows.append(
            ReadRow(
                block_pos=pos,
                index=item.index,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                start_tc=item.start_tc,
                end_tc=item.end_tc,
                text=item.raw_text,
                width=widths[i],
                required_ms=required[i],
                allocated_ms=allocated[i],
                status="ok" if allocated[i] == required[i] else "overdue",
            )
        )
    rows.sort(key=lambda r: (r.start_ms, r.block_pos))
    return rows
