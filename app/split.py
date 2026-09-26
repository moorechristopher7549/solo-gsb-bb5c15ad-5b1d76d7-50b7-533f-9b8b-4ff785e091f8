"""双语 SRT 拆轨：把每块恰好两行的双语字幕拆分为中文轨与英文轨。

规则：

- 每个字幕块正文必须恰好两行：一行归入中文轨，另一行归入英文轨；
  行数不符的块指出块号并拒绝预览/导出。
- 错配计分：汉字落在英文轨、拉丁字母落在中文轨，每个字符各计 1 分；
  数字、标点等中性字符不计分（全角拉丁字母同样计为拉丁字母）。
- 相邻两块的中英行上下顺序发生变化，另计 2 分。
- 每块归属（中文在上 / 英文在上）由全段总代价最小的动态规划确定，
  不做逐块贪心；总代价相同的方案中，从第 1 块起逐块优先「中文在上」，
  使纯数字/标点等中性行依邻块保持轨序稳定。
- 导出的两条 SRT 均保留原时间码与正文行内容，序号从 1 重新编号。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .parsers import SrtItem

ZH_TOP = "zh_top"   # 中文行在上：上行入中文轨、下行入英文轨
EN_TOP = "en_top"   # 英文行在上：上行入英文轨、下行入中文轨

SWITCH_COST = 2       # 相邻块中英行上下顺序变化的额外代价


@dataclass
class SplitRow:
    new_index: int      # 重新编号（从 1 开始）
    index: int          # 原 SRT 声明的序号
    start_tc: str
    end_tc: str
    order: str          # ZH_TOP / EN_TOP
    zh_line: str        # 归入中文轨的正文行（原文）
    en_line: str        # 归入英文轨的正文行（原文）
    mismatch: int       # 本块错配字符数
    switched: bool      # 相对上一块轨序是否变化


def _is_hanzi(ch: str) -> bool:
    """汉字（CJK 统一/兼容表意文字，含〇）。"""
    if ch == "〇":
        return True
    return unicodedata.name(ch, "").startswith(
        ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")
    )


def _is_latin_letter(ch: str) -> bool:
    """拉丁字母（含全角与带附加符号者；不含希腊/西里尔字母）。"""
    if not unicodedata.category(ch).startswith("L"):
        return False
    return "LATIN" in unicodedata.name(ch, "")


def _line_costs(line: str) -> tuple[int, int]:
    """返回 (汉字数, 拉丁字母数)：即该行落入英文轨/中文轨时的错配数。"""
    hanzi = sum(1 for ch in line if _is_hanzi(ch))
    latin = sum(1 for ch in line if _is_latin_letter(ch))
    return hanzi, latin


def _block_label(pos: int, declared: int) -> str:
    """块号描述：解析顺序位置，声明序号不一致时一并给出。"""
    if pos == declared:
        return f"第 {pos} 块"
    return f"第 {pos} 块（声明序号 {declared}）"


def _best_orders(zh_top_costs: list[int], en_top_costs: list[int]) -> list[str]:
    """全段总代价最小的轨序序列；平局从第 1 块起逐块优先「中文在上」。

    后缀 DP：g[i][c] 为第 i..n 块在第 i 块取轨序 c 时的最小总代价
    （含第 i 块自身错配与后续全部顺序变化代价）。随后自第 1 块起逐块
    试探：只要「中文在上」仍能达成全段最优即取之，否则取「英文在上」。
    """
    n = len(zh_top_costs)
    costs = (zh_top_costs, en_top_costs)
    g = [[0, 0] for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for c in (0, 1):
            g[i][c] = costs[c][i] + min(
                g[i + 1][c], g[i + 1][1 - c] + SWITCH_COST
            )
    best = min(g[0][0], g[0][1])
    orders: list[str] = []
    acc = 0      # 已确定前缀（不含本块）的实际代价
    prev = -1    # 上一块轨序：0 中文在上 / 1 英文在上 / -1 尚无
    for i in range(n):
        pick = 1
        for c in (0, 1):   # 0 = 中文在上，优先试探
            total = acc + (SWITCH_COST if prev not in (-1, c) else 0) + g[i][c]
            if total == best:
                pick = c
                break
        orders.append(ZH_TOP if pick == 0 else EN_TOP)
        acc += (SWITCH_COST if prev not in (-1, pick) else 0) + costs[pick][i]
        prev = pick
    return orders


def split_subtitles(items: list[SrtItem]) -> list[SplitRow]:
    """逐块拆分双语字幕；任一块正文不是恰好两行时抛 ValueError（指出块号）。"""
    if not items:
        return []

    block_lines: list[tuple[str, str]] = []
    zh_top_costs: list[int] = []
    en_top_costs: list[int] = []
    for pos, item in enumerate(items, start=1):
        lines = list(item.body_lines) or [item.raw_text]
        if len(lines) != 2:
            raise ValueError(
                f"{_block_label(pos, item.index)}正文须恰好两行"
                f"（实际 {len(lines)} 行），无法拆轨"
            )
        top_hanzi, top_latin = _line_costs(lines[0])
        bot_hanzi, bot_latin = _line_costs(lines[1])
        # 中文在上：上行入中文轨（计拉丁字母）、下行入英文轨（计汉字）
        zh_top_costs.append(top_latin + bot_hanzi)
        # 英文在上：上行入英文轨（计汉字）、下行入中文轨（计拉丁字母）
        en_top_costs.append(top_hanzi + bot_latin)
        block_lines.append((lines[0], lines[1]))

    orders = _best_orders(zh_top_costs, en_top_costs)
    rows: list[SplitRow] = []
    prev_order = ""
    for pos, (item, (top, bottom), order) in enumerate(
        zip(items, block_lines, orders), start=1
    ):
        zh_line, en_line = (top, bottom) if order == ZH_TOP else (bottom, top)
        mismatch = (
            zh_top_costs if order == ZH_TOP else en_top_costs
        )[pos - 1]
        rows.append(
            SplitRow(
                new_index=pos,
                index=item.index,
                start_tc=item.start_tc,
                end_tc=item.end_tc,
                order=order,
                zh_line=zh_line,
                en_line=en_line,
                mismatch=mismatch,
                switched=bool(prev_order) and order != prev_order,
            )
        )
        prev_order = order
    return rows


def build_split_srt(rows: list[SplitRow], track: str) -> str:
    """生成单轨 SRT：保留原时间码与正文行内容，序号从 1 重新编号。"""
    if track not in ("zh", "en"):
        raise ValueError(f"未知轨道：{track!r}（应为 zh 或 en）")
    blocks = []
    for row in rows:
        line = row.zh_line if track == "zh" else row.en_line
        blocks.append(
            f"{row.new_index}\n{row.start_tc} --> {row.end_tc}\n{line}"
        )
    return "\n\n".join(blocks) + "\n"
