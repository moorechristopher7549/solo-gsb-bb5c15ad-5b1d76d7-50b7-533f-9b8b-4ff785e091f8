"""双语术语核对：在双语 SRT 英文轨中定位术语，核对对应中文轨是否使用指定译法。

规则：

- 输入为每块恰好两行的双语 SRT（复用拆轨逻辑分出中文轨/英文轨）与逐行
  「英文术语<TAB>指定中文译法」词表；词表空项（缺 TAB、英文术语或指定
  译法为空）与归一化后重复的英文术语都会指出行号并拒绝核对。
- 英文轨仅跨「相邻且间隔 0～500 毫秒」的块拼接成段（块间补一个空格），
  在拼接段内查找术语；间隔超出范围的块各自成段，不跨段匹配。
- 匹配前对英文做归一化：NFKC（全角→半角等）→ 小写折叠 → 连续空白折叠为
  单个空格；按英文词边界（命中两侧不允许是英文字母或数字）查找术语。
- 重叠候选先选覆盖字符最多者，再按出现位置、词表顺序；同一英文字符只
  归属一次（被较长命中占用后，与其重叠的更短候选丢弃）。
- 命中覆盖的每个块都要核对：该块中文轨去空白后包含指定译法（同样去空白）
  即合格，否则记为缺译，标出块号、英文原文与应译词。
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass

from .parsers import SrtItem
from .split import SplitRow

MAX_CHAIN_GAP_MS = 500   # 相邻块英文轨可拼接的最大间隔（毫秒）

_WS_RE = re.compile(r"\s+")


@dataclass
class TermEntry:
    order: int        # 词表条目序位（从 1 开始，空白行不计）
    line_no: int      # 原文物理行号（从 1 开始）
    en_raw: str       # 英文术语原文（去除首尾空白）
    zh_raw: str       # 指定中文译法原文（去除首尾空白）
    en_norm: str      # 归一化后的英文术语（用于匹配与去重）
    zh_key: str       # 去空白后的指定译法（用于包含判定）


@dataclass
class CheckRow:
    status: str       # "ok" 合格 / "missing" 缺译
    block_pos: int    # 块号：SRT 解析序位（从 1 开始）
    block_index: int  # SRT 块内声明的序号
    start_tc: str
    end_tc: str
    en_line: str      # 该块英文轨原文
    zh_line: str      # 该块中文轨原文
    term: str         # 词表英文术语原文
    matched: str      # 归一化英文轨中实际命中的文本
    expected_zh: str  # 应译词（词表指定中文译法原文）


@dataclass
class TermCheckResult:
    rows: list[CheckRow]
    chains: int       # 英文轨拼接段数
    hits: int         # 去重后的命中次数（一次命中可覆盖多个块）


def normalize_english(text: str) -> str:
    """英文归一化：NFKC（全角→半角等）→ 小写折叠 → 连续空白折叠为单个空格。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    return _WS_RE.sub(" ", text).strip()


def _strip_ws(text: str) -> str:
    """去除全部空白字符（含全角空格）。"""
    return _WS_RE.sub("", text)


def parse_glossary(text: str) -> list[TermEntry]:
    """解析逐行「英文术语<TAB>指定中文译法」词表。

    空白行跳过；缺 TAB、英文术语或指定译法为空的行，以及归一化后重复的
    英文术语，都抛出 ValueError 并指出行号。
    """
    entries: list[TermEntry] = []
    seen: dict[str, int] = {}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        en, sep, zh = line.partition("\t")
        if not sep:
            raise ValueError(
                f"词表第 {line_no} 行缺少制表符（TAB）："
                "每行须为「英文术语<TAB>指定中文译法」"
            )
        en_raw, zh_raw = en.strip(), zh.strip()
        if not en_raw:
            raise ValueError(f"词表第 {line_no} 行英文术语为空")
        if not zh_raw:
            raise ValueError(f"词表第 {line_no} 行指定中文译法为空")
        en_norm = normalize_english(en_raw)
        if not en_norm:
            raise ValueError(f"词表第 {line_no} 行英文术语归一化后为空")
        if en_norm in seen:
            raise ValueError(
                f"词表第 {line_no} 行英文术语「{en_raw}」"
                f"与第 {seen[en_norm]} 行重复（归一化后相同）"
            )
        seen[en_norm] = line_no
        entries.append(
            TermEntry(
                order=len(entries) + 1,
                line_no=line_no,
                en_raw=en_raw,
                zh_raw=zh_raw,
                en_norm=en_norm,
                zh_key=_strip_ws(zh_raw),
            )
        )
    return entries


def _is_word_char(ch: str) -> bool:
    """英文词字符：ASCII 字母或数字（等价于字符类 [0-9A-Za-z]）。"""
    return ch.isascii() and ch.isalnum()


def _find_matches(
    text: str, entries: list[TermEntry]
) -> list[tuple[int, int, TermEntry, str]]:
    """在归一化文本中查找全部术语命中，按命中起点升序返回。

    按英文词边界匹配（命中两侧不允许是英文字母或数字）；重叠候选先选
    覆盖字符最多者，再按出现位置（起点），最后按词表顺序；同一英文
    字符只归属一次。
    """
    candidates: list[tuple[int, int, TermEntry, str]] = []
    for entry in entries:
        term = entry.en_norm
        tlen = len(term)
        pos = text.find(term)
        while pos != -1:
            end = pos + tlen
            if (pos == 0 or not _is_word_char(text[pos - 1])) and (
                end == len(text) or not _is_word_char(text[end])
            ):
                candidates.append((pos, end, entry, term))
            pos = text.find(term, pos + 1)
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0], c[2].order))

    accepted: list[tuple[int, int, TermEntry, str]] = []   # 按起点升序、互不重叠
    starts: list[int] = []
    for start, end, entry, matched in candidates:
        k = bisect.bisect_left(starts, start)
        if k > 0 and accepted[k - 1][1] > start:
            continue    # 与前一个已接受命中重叠
        if k < len(accepted) and accepted[k][0] < end:
            continue    # 与后一个已接受命中重叠
        accepted.insert(k, (start, end, entry, matched))
        starts.insert(k, start)
    return accepted


@dataclass
class _Chain:
    positions: list[int]          # 段内各块的解析序位（从 1 开始）
    text: str                     # 归一化拼接文本（块间一个空格）
    spans: list[tuple[int, int]]  # 各块在拼接文本中的字符区间 [start, end)


def _build_chains(items: list[SrtItem], rows: list[SplitRow]) -> list[_Chain]:
    """把相邻且间隔 0～500 毫秒的块的英文轨拼接成段（块间补一个空格）。"""
    groups: list[list[int]] = []
    for i in range(len(rows)):
        gap = items[i].start_ms - items[i - 1].end_ms if i else -1
        if groups and 0 <= gap <= MAX_CHAIN_GAP_MS:
            groups[-1].append(i)
        else:
            groups.append([i])
    chains: list[_Chain] = []
    for group in groups:
        parts: list[str] = []
        spans: list[tuple[int, int]] = []
        pos = 0
        for i in group:
            norm = normalize_english(rows[i].en_line)
            if parts:
                parts.append(" ")
                pos += 1
            spans.append((pos, pos + len(norm)))
            parts.append(norm)
            pos += len(norm)
        chains.append(
            _Chain(
                positions=[i + 1 for i in group],
                text="".join(parts),
                spans=spans,
            )
        )
    return chains


def check_terms(
    items: list[SrtItem], rows: list[SplitRow], entries: list[TermEntry]
) -> TermCheckResult:
    """核对全部命中：命中覆盖的每个块，其中文轨去空白后须包含指定译法。

    ``items`` 与 ``rows`` 一一对应（同一份 SRT 的解析结果与拆轨结果）。
    """
    chains = _build_chains(items, rows)
    zh_keys = [_strip_ws(r.zh_line) for r in rows]
    out: list[CheckRow] = []
    hits = 0
    for chain in chains:
        matches = _find_matches(chain.text, entries)
        if not matches:
            continue
        hits += len(matches)
        span_starts = [s for s, _ in chain.spans]
        for start, end, entry, matched in matches:
            # 各块 span 互不重叠且按起点升序；只检查可能与命中区间相交的块
            j = max(bisect.bisect_right(span_starts, start) - 1, 0)
            while j < len(chain.positions):
                b_start, b_end = chain.spans[j]
                if b_start >= end:
                    break
                if b_end > start:
                    pos = chain.positions[j]
                    row = rows[pos - 1]
                    status = (
                        "ok" if entry.zh_key in zh_keys[pos - 1] else "missing"
                    )
                    out.append(
                        CheckRow(
                            status=status,
                            block_pos=pos,
                            block_index=row.index,
                            start_tc=row.start_tc,
                            end_tc=row.end_tc,
                            en_line=row.en_line,
                            zh_line=row.zh_line,
                            term=entry.en_raw,
                            matched=matched,
                            expected_zh=entry.zh_raw,
                        )
                    )
                j += 1
    return TermCheckResult(rows=out, chains=len(chains), hits=hits)
