"""字幕阅读负载审查：把所有字幕块放到同一条阅读时间轴上模拟分配。

规则：

- 正文去掉所有空白后按显示宽度计量：东亚宽字符（East Asian Width
  为 W/F）计 2，其余字符计 1；每块所需阅读毫秒数为
  ``ceil(宽度单位 × 1000 / 阅读速度)``。
- 阅读只允许发生在块自身的 ``[入点, 出点)`` 区间内（左闭右开）。
- 所有块放在同一阅读时间轴上，**同一时刻只读一块**；按
  **最早出点优先**分配，出点相同时按**解析序位**（EDF，可抢占）：
  某块入点、某块出点或当前块读完时立即重新挑选可读块。
- 块到出点仍未读完即**逾期**，未分配毫秒数 = 所需 − 已分配；
  入点不早于出点的块无法安排阅读，由调用方指出块号并拒绝预览/导出。
"""
from __future__ import annotations

import heapq
import unicodedata
from dataclasses import dataclass

from .parsers import SrtItem

STATUS_OK = "ok"            # 在自身 [入点, 出点) 内读完
STATUS_OVERDUE = "overdue"  # 到出点仍未读完


@dataclass
class ReadingRow:
    """单个字幕块的阅读负载审查结果（按解析序位生成）。"""

    pos: int            # 解析序位（从 1 开始）
    index: int          # SRT 声明序号
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    text: str           # 正文（多行以空格连接，供预览）
    width: int          # 去空白后的显示宽度单位数
    required_ms: int    # 所需阅读毫秒（向上取整）
    allocated_ms: int   # 在 [入点, 出点) 内实际分配到的阅读毫秒

    @property
    def unallocated_ms(self) -> int:
        return self.required_ms - self.allocated_ms

    @property
    def status(self) -> str:
        return STATUS_OK if self.unallocated_ms == 0 else STATUS_OVERDUE


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def display_width(text: str) -> int:
    """去掉全部空白后的显示宽度：东亚宽字符（W/F）计 2，其余计 1。"""
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def required_reading_ms(width: int, speed: int) -> int:
    """所需阅读毫秒数：``width`` 个宽度单位按 ``speed`` 单位/秒阅读，向上取整。"""
    if width <= 0:
        return 0
    return (width * 1000 + speed - 1) // speed


def review_reading_load(
    items: list[SrtItem], speed: int
) -> list[ReadingRow]:
    """模拟单通道最早出点优先（EDF）的阅读时间分配。

    事件点为每块的入点（放行）、出点（截止）与当前块读完的时刻；
    任一事件发生后重新挑选「已放行、未读完、未逾期」中出点最早、
    出点相同则解析序位最靠前的块。返回按解析序位排列的逐块结果；
    存在入点不早于出点的块时抛出 ValueError 并指出块号。
    """
    n = len(items)
    rows: list[ReadingRow] = []
    for pos, item in enumerate(items, start=1):
        if item.start_ms >= item.end_ms:
            raise ValueError(
                f"{_block_label(pos, item.index)}入点不早于出点"
                f"（{item.start_tc} --> {item.end_tc}），无法安排阅读"
            )
        width = display_width(item.raw_text)
        rows.append(
            ReadingRow(
                pos=pos,
                index=item.index,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                start_tc=item.start_tc,
                end_tc=item.end_tc,
                text=item.raw_text,
                width=width,
                required_ms=required_reading_ms(width, speed),
                allocated_ms=0,
            )
        )

    remaining = [r.required_ms for r in rows]
    # 放行顺序：入点升序，同入点按解析序位
    release_order = sorted(range(n), key=lambda i: (rows[i].start_ms, i))
    next_release = 0
    # 可读块堆：(出点, 解析序位下标 0 起)
    ready: list[tuple[int, int]] = []
    t = 0

    while next_release < n or ready:
        # 放行所有入点 <= t 的块（无需阅读的 0 宽块直接合格）
        while next_release < n and rows[release_order[next_release]].start_ms <= t:
            i = release_order[next_release]
            if remaining[i] > 0:
                heapq.heappush(ready, (rows[i].end_ms, i))
            next_release += 1

        if not ready:
            # 时间轴空闲：直接跳到下一个入点
            t = rows[release_order[next_release]].start_ms
            continue

        # 出点 <= t 的块窗口已关闭（左闭右开），未读完部分记为逾期
        while ready and ready[0][0] <= t:
            heapq.heappop(ready)
        if not ready:
            continue

        end_ms, i = ready[0]
        next_t = min(end_ms, t + remaining[i])
        if next_release < n:
            next_t = min(next_t, rows[release_order[next_release]].start_ms)

        allocated = next_t - t
        rows[i].allocated_ms += allocated
        remaining[i] -= allocated
        t = next_t
        if remaining[i] == 0:
            heapq.heappop(ready)

    return rows
