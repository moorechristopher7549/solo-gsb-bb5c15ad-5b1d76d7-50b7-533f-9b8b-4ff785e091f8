"""剪辑字幕重建：按素材剪辑区间裁切原 SRT 字幕并平移到目标时间轴。

规则：

- 剪辑清单每项含非负整数毫秒 ``source_in`` / ``source_out`` / ``target_in``，
  出点须大于入点；片段可乱序、重复，输出（目标）区间可重叠，按原速（1:1）映射：
  ``target = target_in + (source - source_in)``。
- 将每个字幕块与每段素材区间按**左闭右开**求交：交集为空直接丢弃；非空交集
  裁切后整体平移到目标位置。
- 按映射后的全部端点切分时间轴，每段把当前有效的原字幕块按**解析序位**排列，
  保留各块正文换行，块与块之间再以换行连接。
- 同一原块被多段剪辑覆盖时只显示一次（覆盖计数），直到**最后一份覆盖结束**
  才移除；因此覆盖区间重叠/相接处不会重复出现同一正文。
- 仅合并**相邻且有效原块集合相同**的区间，无字幕区间跳过（也阻断跨空隙合并）；
  导出按时间从 1 编号、互不重叠的 SRT。
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Any

from .parsers import SrtItem, format_tc

CLIP_FIELDS = ("source_in", "source_out", "target_in")


@dataclass(frozen=True)
class Clip:
    source_in: int      # 素材入点（毫秒，含）
    source_out: int     # 素材出点（毫秒，不含），须大于 source_in
    target_in: int      # 目标入点（毫秒，含）


@dataclass
class RebuildRow:
    new_index: int                  # 导出序号（按时间从 1 开始）
    start_ms: int
    end_ms: int
    start_tc: str
    end_tc: str
    source_indices: tuple[int, ...]     # 有效原块的解析序位（从 1 开始，升序）
    body_lines: tuple[str, ...]         # 合并后的正文行（保留块内换行）


def validate_clips(raw: Any) -> list[Clip]:
    """把 JSON 剪辑数组校验并转换为 Clip；任一项非法时抛 ValueError 并指出项号。"""
    if not isinstance(raw, list):
        raise ValueError("剪辑清单须为 JSON 数组，每项含 source_in/source_out/target_in")
    clips: list[Clip] = []
    for n, obj in enumerate(raw, start=1):
        if not isinstance(obj, dict):
            raise ValueError(f"第 {n} 项须为 JSON 对象")
        vals: dict[str, int] = {}
        for key in CLIP_FIELDS:
            if key not in obj:
                raise ValueError(f"第 {n} 项缺少字段 {key}")
            v = obj[key]
            # 布尔值是 int 的子类，剪辑时间不接受；浮点/字符串等同样拒绝
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(
                    f"第 {n} 项 {key} 须为非负整数毫秒（实际收到 {v!r}）"
                )
            if v < 0:
                raise ValueError(f"第 {n} 项 {key} 不能为负（实际 {v} 毫秒）")
            vals[key] = v
        if vals["source_out"] <= vals["source_in"]:
            raise ValueError(
                f"第 {n} 项 source_out（{vals['source_out']} 毫秒）须大于 "
                f"source_in（{vals['source_in']} 毫秒）"
            )
        clips.append(
            Clip(vals["source_in"], vals["source_out"], vals["target_in"])
        )
    if not clips:
        raise ValueError("剪辑清单为空，请至少提供一段剪辑区间")
    return clips


def rebuild_subtitles(
    items: list[SrtItem], clips: list[Clip]
) -> list[RebuildRow]:
    """按剪辑区间重建字幕时间轴。items 为 parse_srt 的结果，clips 已通过校验。"""
    n = len(items)
    if not items or not clips:
        return []

    # 按原入点排序以便二分裁剪候选块（解析序位仍用 items 中的位置 idx+1）
    order = sorted(range(n), key=lambda i: (items[i].start_ms, i))
    starts = [items[i].start_ms for i in order]

    # (时间, 增量, 原块解析序位)：同一块在多段剪辑中的覆盖用计数合并
    events: list[tuple[int, int, int]] = []
    for clip in clips:
        # start_ms < source_out 的块才可能与本段素材区间相交（左闭右开）
        k = bisect_left(starts, clip.source_out)
        for j in range(k):
            idx = order[j]
            item = items[idx]
            lo = max(item.start_ms, clip.source_in)
            hi = min(item.end_ms, clip.source_out)
            if lo >= hi:
                continue  # 空交集丢弃（含仅端点相触的情形）
            pos = idx + 1
            events.append(
                (clip.target_in + (lo - clip.source_in), 1, pos)
            )
            events.append(
                (clip.target_in + (hi - clip.source_in), -1, pos)
            )

    events.sort(key=lambda e: e[0])

    rows: list[RebuildRow] = []
    active: dict[int, int] = {}   # 原块序位 -> 当前覆盖份数
    prev_t: int | None = None
    i = 0
    total = len(events)
    while i < total:
        t = events[i][0]
        # [prev_t, t) 内有效的原块集合为 active（处理 t 处事件之前的状态）
        if prev_t is not None and t > prev_t and active:
            _append_or_merge(rows, items, prev_t, t, active)
        while i < total and events[i][0] == t:
            _, delta, pos = events[i]
            count = active.get(pos, 0) + delta
            if count:
                active[pos] = count
            else:
                active.pop(pos, None)   # 最后一份覆盖结束才移除
            i += 1
        prev_t = t

    for seq, row in enumerate(rows, start=1):
        row.new_index = seq
    return rows


def _append_or_merge(
    rows: list[RebuildRow],
    items: list[SrtItem],
    start_ms: int,
    end_ms: int,
    active: dict[int, int],
) -> None:
    """追加一个有效区间；与上一段相邻且有效原块集合相同时就地合并。"""
    positions = tuple(sorted(active))
    if (
        rows
        and rows[-1].end_ms == start_ms   # 相邻：中间不得夹着无字幕空隙
        and rows[-1].source_indices == positions
    ):
        rows[-1].end_ms = end_ms
        rows[-1].end_tc = format_tc(end_ms)
        return
    body_lines: list[str] = []
    for pos in positions:                 # 按解析序位排列各有效原块
        item = items[pos - 1]
        body_lines.extend(item.body_lines or (item.raw_text,))
    rows.append(
        RebuildRow(
            new_index=0,
            start_ms=start_ms,
            end_ms=end_ms,
            start_tc=format_tc(start_ms),
            end_tc=format_tc(end_ms),
            source_indices=positions,
            body_lines=tuple(body_lines),
        )
    )


def build_rebuild_srt(rows: list[RebuildRow]) -> str:
    """生成重建后的 SRT：按时间从 1 编号、区间互不重叠，正文保留块内换行。"""
    blocks = []
    for row in rows:
        lines = [str(row.new_index), f"{row.start_tc} --> {row.end_tc}"]
        lines.extend(row.body_lines)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
