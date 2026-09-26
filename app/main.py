"""字幕校稿 Web 应用：FastAPI 后端。"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alignment import GAP_COST, align, align_with_anchors, normalize_anchors
from .avoid import avoid_subtitles, build_avoid_srt
from .diffalign import diff_align, time_dev_ms
from .parsers import parse_script, parse_srt
from .reading import review_reading_load
from .rebuild import build_rebuild_srt, rebuild_subtitles, validate_clips
from .relayout import build_relayout_srt, relayout_subtitles
from .retime import CalibPoint, build_srt, parse_timecode, retime_subtitles
from .split import SWITCH_COST, build_split_srt, split_subtitles
from .termcheck import check_terms, parse_glossary
from .textnorm import normalize_text

MAX_ITEMS = 2000          # 每侧条目数上限，防止 DP 规模过大导致请求过久
MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_POINTS = 500          # 重定时校准点数量上限
MIN_LINE_WIDTH = 12       # 断行排版每行最大显示宽度下限
MAX_LINE_WIDTH = 60       # 断行排版每行最大显示宽度上限
MIN_GAP_MS = 0            # 时间避让最小块间隔下限（毫秒）
MAX_GAP_MS = 60_000       # 时间避让最小块间隔上限（毫秒）
MIN_SHIFT_MS = 0          # 时间避让单块最大平移量下限（毫秒）
MAX_SHIFT_MS = 3_600_000  # 时间避让单块最大平移量上限（毫秒）
MAX_CLIPS = 2000          # 剪辑字幕重建素材片段数上限
MAX_TERMS = 2000          # 双语术语核对词表条目数上限
MIN_READING_SPEED = 4     # 阅读速度下限（显示宽度单位/秒）
MAX_READING_SPEED = 30    # 阅读速度上限（显示宽度单位/秒）

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="字幕校稿对齐", version="1.0.0")


class Anchor(BaseModel):
    script: int = Field(description="对白稿行序位（解析后非空行，从 1 开始）")
    srt: int = Field(description="SRT 块序位（解析后字幕块，从 1 开始）")


class AlignRequest(BaseModel):
    script: str = Field(default="", description="逐行对白稿")
    srt: str = Field(default="", description="SRT 字幕")
    gap_cost: float = Field(default=GAP_COST, ge=0.0, le=1.0)
    anchors: list[Anchor] | None = Field(
        default=None, description="人工锚点：强制配对的 行:块 序位对"
    )


def _build_rows(
    script_text: str,
    srt_text: str,
    gap_cost: float,
    anchors: list[Anchor] | None = None,
):
    if len(script_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "对白稿超过 2 MB 大小限制")
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")

    try:
        scripts = parse_script(script_text)
    except Exception as exc:  # 纯文本解析基本不会失败，兜底
        raise HTTPException(400, f"对白稿解析失败：{exc}") from exc
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc

    if not scripts and not subs:
        raise HTTPException(400, "两侧内容均为空，请粘贴对白稿与 SRT")
    if len(scripts) > MAX_ITEMS or len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"条目数超过每侧 {MAX_ITEMS} 条的上限，请拆分后再对齐"
        )

    norms_s = [normalize_text(it.raw_text) for it in scripts]
    norms_t = [normalize_text(it.raw_text) for it in subs]

    anchor_pairs: list[tuple[int, int]] = []
    if anchors:
        try:
            anchor_pairs = normalize_anchors(
                [(a.script, a.srt) for a in anchors], len(scripts), len(subs)
            )
        except ValueError as exc:
            raise HTTPException(400, f"锚点无效：{exc}") from exc
        ops = align_with_anchors(norms_s, norms_t, anchor_pairs, gap_cost=gap_cost)
    else:
        ops = align(norms_s, norms_t, gap_cost=gap_cost)

    rows = []
    totals = {"match": 0, "missing_script": 0, "missing_srt": 0}
    match_cost_sum = 0.0
    for op, cost, i, j in ops:
        if op in ("match", "anchor"):
            s, t = scripts[i], subs[j]
            row = {
                "status": "match",
                "cost": round(cost, 6),
                "script_index": s.index,
                "script_text": s.raw_text,
                "srt_index": t.index,
                "srt_text": t.raw_text,
                "start_tc": t.start_tc,
                "end_tc": t.end_tc,
            }
            if anchor_pairs:
                # 仅锚点模式下输出该标记，未提供锚点时保持原有字段不变
                row["anchor"] = op == "anchor"
            totals["match"] += 1
            match_cost_sum += cost
        elif op == "gap_a":
            # 对白稿有条目，SRT 无对应字幕
            s = scripts[i]
            row = {
                "status": "missing_srt",
                "cost": round(cost, 6),
                "script_index": s.index,
                "script_text": s.raw_text,
                "srt_index": None,
                "srt_text": "",
                "start_tc": "",
                "end_tc": "",
            }
            totals["missing_srt"] += 1
        else:
            # SRT 有条目，对白稿缺失
            t = subs[j]
            row = {
                "status": "missing_script",
                "cost": round(cost, 6),
                "script_index": None,
                "script_text": "",
                "srt_index": t.index,
                "srt_text": t.raw_text,
                "start_tc": t.start_tc,
                "end_tc": t.end_tc,
            }
            totals["missing_script"] += 1
        rows.append(row)

    total_cost = sum(r["cost"] for r in rows)
    summary = {
        **totals,
        "total_rows": len(rows),
        "total_cost": round(total_cost, 6),
        "gap_cost": gap_cost,
        "avg_match_cost": round(match_cost_sum / totals["match"], 6)
        if totals["match"]
        else None,
    }
    if anchor_pairs:
        summary["anchors"] = len(anchor_pairs)
    return rows, summary


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/align")
def api_align(req: AlignRequest):
    rows, summary = _build_rows(req.script, req.srt, req.gap_cost, req.anchors)
    return {"summary": summary, "rows": rows}


@app.post("/api/export.csv")
def api_export_csv(req: AlignRequest):
    rows, _ = _build_rows(req.script, req.srt, req.gap_cost, req.anchors)
    use_anchors = bool(req.anchors)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    header = [
        "状态", "代价", "对白稿行号", "对白稿文本",
        "SRT序号", "时间码入点", "时间码出点", "SRT文本",
    ]
    if use_anchors:
        header.append("锚点")
    writer.writerow(header)
    status_label = {
        "match": "对位",
        "missing_script": "缺对白稿",
        "missing_srt": "缺字幕",
    }
    for r in rows:
        values = [
            status_label[r["status"]],
            f"{r['cost']:.4f}",
            r["script_index"] if r["script_index"] is not None else "",
            r["script_text"],
            r["srt_index"] if r["srt_index"] is not None else "",
            r["start_tc"],
            r["end_tc"],
            r["srt_text"],
        ]
        if use_anchors:
            values.append("锚点" if r.get("anchor") else "")
        writer.writerow(values)
    # UTF-8 BOM，保证 Excel 直接打开中文不乱码
    data = "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="alignment.csv"'
        },
    )


class CalibPointIn(BaseModel):
    source: str = Field(description="原时间码，HH:MM:SS,mmm")
    target: str = Field(description="目标时间码，HH:MM:SS,mmm")


class RetimeRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    points: list[CalibPointIn] = Field(
        default_factory=list,
        description="按顺序排列的校准点（原时间→目标时间），两列时间均须严格递增",
    )


def _build_retime_rows(srt_text: str, points_in: list[CalibPointIn]):
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再重定时"
        )
    if not points_in:
        raise HTTPException(400, "至少需要一个校准点")
    if len(points_in) > MAX_POINTS:
        raise HTTPException(400, f"校准点数量超过 {MAX_POINTS} 的上限")

    points: list[CalibPoint] = []
    for i, p in enumerate(points_in, start=1):
        try:
            points.append(
                CalibPoint(parse_timecode(p.source), parse_timecode(p.target))
            )
        except ValueError as exc:
            raise HTTPException(400, f"第 {i} 个校准点：{exc}") from exc
    try:
        return retime_subtitles(subs, points)
    except ValueError as exc:
        raise HTTPException(400, f"重定时结果校验失败：{exc}") from exc


@app.post("/api/retime")
def api_retime(req: RetimeRequest):
    rows = _build_retime_rows(req.srt, req.points)
    return {
        "summary": {"total": len(rows), "points": len(req.points)},
        "rows": [
            {
                "new_index": r.new_index,
                "index": r.index,
                "old_start_tc": r.old_start_tc,
                "old_end_tc": r.old_end_tc,
                "new_start_tc": r.new_start_tc,
                "new_end_tc": r.new_end_tc,
                "text": r.text,
            }
            for r in rows
        ],
    }


@app.post("/api/retime.srt")
def api_retime_srt(req: RetimeRequest):
    rows = _build_retime_rows(req.srt, req.points)
    data = build_srt(rows).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="retimed.srt"'
        },
    )


class RelayoutRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    max_width: int = Field(
        default=30,
        ge=MIN_LINE_WIDTH,
        le=MAX_LINE_WIDTH,
        description="每行最大显示宽度（12~60，东亚宽字符计 2，其余计 1）",
    )


def _build_relayout_rows(srt_text: str, max_width: int):
    if not MIN_LINE_WIDTH <= max_width <= MAX_LINE_WIDTH:
        raise HTTPException(
            400,
            f"每行最大显示宽度须在 {MIN_LINE_WIDTH}~{MAX_LINE_WIDTH} 之间",
        )
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再排版"
        )
    return relayout_subtitles(subs, max_width)


def _relayout_row_json(r) -> dict:
    return {
        "index": r.index,
        "start_tc": r.start_tc,
        "end_tc": r.end_tc,
        "original_text": r.original_text,
        "result_text": r.result_text,
        "line_count": r.line_count,
        "status": r.status,
        "reason": r.reason,
    }


@app.post("/api/relayout")
def api_relayout(req: RelayoutRequest):
    rows = _build_relayout_rows(req.srt, req.max_width)
    counts = {"single": 0, "wrapped": 0, "manual": 0}
    for r in rows:
        counts[r.status] += 1
    return {
        "summary": {
            "total": len(rows),
            "max_width": req.max_width,
            "single": counts["single"],
            "wrapped": counts["wrapped"],
            "manual": counts["manual"],
        },
        "rows": [_relayout_row_json(r) for r in rows],
    }


@app.post("/api/relayout.srt")
def api_relayout_srt(req: RelayoutRequest):
    rows = _build_relayout_rows(req.srt, req.max_width)
    data = build_relayout_srt(rows).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="relayout.srt"'
        },
    )


class AvoidRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    min_gap_ms: int = Field(
        default=0,
        ge=MIN_GAP_MS,
        le=MAX_GAP_MS,
        description="最小块间隔（毫秒，0~60000）：相邻块出点到下一块入点的最小距离",
    )
    max_shift_ms: int = Field(
        default=2000,
        ge=MIN_SHIFT_MS,
        le=MAX_SHIFT_MS,
        description="单块最大平移量（毫秒，0~3600000）：每块入点允许移动的最大绝对距离",
    )


def _build_avoid_rows(srt_text: str, min_gap_ms: int, max_shift_ms: int):
    if not MIN_GAP_MS <= min_gap_ms <= MAX_GAP_MS:
        raise HTTPException(
            400, f"最小块间隔须在 {MIN_GAP_MS}~{MAX_GAP_MS} 毫秒之间"
        )
    if not MIN_SHIFT_MS <= max_shift_ms <= MAX_SHIFT_MS:
        raise HTTPException(
            400, f"单块最大平移量须在 {MIN_SHIFT_MS}~{MAX_SHIFT_MS} 毫秒之间"
        )
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再避让"
        )
    try:
        return avoid_subtitles(subs, min_gap_ms, max_shift_ms)
    except ValueError as exc:
        raise HTTPException(400, f"时间避让失败：{exc}") from exc


def _avoid_row_json(r) -> dict:
    return {
        "new_index": r.new_index,
        "index": r.index,
        "old_start_tc": r.old_start_tc,
        "old_end_tc": r.old_end_tc,
        "new_start_tc": r.new_start_tc,
        "new_end_tc": r.new_end_tc,
        "shift_ms": r.shift_ms,
        "text": r.text,
    }


@app.post("/api/avoid")
def api_avoid(req: AvoidRequest):
    rows = _build_avoid_rows(req.srt, req.min_gap_ms, req.max_shift_ms)
    return {
        "summary": {
            "total": len(rows),
            "min_gap_ms": req.min_gap_ms,
            "max_shift_ms": req.max_shift_ms,
            "max_abs_shift_ms": max((abs(r.shift_ms) for r in rows), default=0),
            "moved": sum(1 for r in rows if r.shift_ms != 0),
        },
        "rows": [_avoid_row_json(r) for r in rows],
    }


@app.post("/api/avoid.srt")
def api_avoid_srt(req: AvoidRequest):
    rows = _build_avoid_rows(req.srt, req.min_gap_ms, req.max_shift_ms)
    data = build_avoid_srt(rows).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="avoid.srt"'
        },
    )


class SplitRequest(BaseModel):
    srt: str = Field(
        default="", description="双语 SRT 字幕（每块正文须恰好两行）"
    )


def _build_split_rows(srt_text: str):
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再拆轨"
        )
    try:
        return split_subtitles(subs)
    except ValueError as exc:
        raise HTTPException(400, f"拆轨失败：{exc}") from exc


def _split_row_json(r) -> dict:
    return {
        "new_index": r.new_index,
        "index": r.index,
        "start_tc": r.start_tc,
        "end_tc": r.end_tc,
        "order": r.order,
        "zh_line": r.zh_line,
        "en_line": r.en_line,
        "mismatch": r.mismatch,
        "switched": r.switched,
    }


@app.post("/api/split")
def api_split(req: SplitRequest):
    rows = _build_split_rows(req.srt)
    mismatch = sum(r.mismatch for r in rows)
    switches = sum(1 for r in rows if r.switched)
    return {
        "summary": {
            "total": len(rows),
            "mismatch_chars": mismatch,
            "switches": switches,
            "switch_cost": SWITCH_COST * switches,
            "total_cost": mismatch + SWITCH_COST * switches,
        },
        "rows": [_split_row_json(r) for r in rows],
    }


def _split_srt_response(rows, track: str, filename: str) -> Response:
    data = build_split_srt(rows, track).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/split.zh.srt")
def api_split_zh_srt(req: SplitRequest):
    rows = _build_split_rows(req.srt)
    return _split_srt_response(rows, "zh", "split.zh.srt")


@app.post("/api/split.en.srt")
def api_split_en_srt(req: SplitRequest):
    rows = _build_split_rows(req.srt)
    return _split_srt_response(rows, "en", "split.en.srt")


class TermCheckRequest(BaseModel):
    srt: str = Field(
        default="", description="双语 SRT 字幕（每块正文须恰好两行）"
    )
    glossary: str = Field(
        default="", description="术语词表：每行 英文术语<TAB>指定中文译法"
    )


def _build_termcheck_rows(srt_text: str, glossary_text: str):
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    if len(glossary_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "词表超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再核对"
        )
    try:
        entries = parse_glossary(glossary_text)
    except ValueError as exc:
        raise HTTPException(400, f"词表无效：{exc}") from exc
    if not entries:
        raise HTTPException(400, "词表内容为空，请粘贴术语词表")
    if len(entries) > MAX_TERMS:
        raise HTTPException(
            400, f"词表条目数超过 {MAX_TERMS} 的上限，请拆分后再核对"
        )
    try:
        split_rows = split_subtitles(subs)
    except ValueError as exc:
        raise HTTPException(400, f"拆轨失败：{exc}") from exc
    result = check_terms(subs, split_rows, entries)
    summary = {
        "total_blocks": len(split_rows),
        "chains": result.chains,
        "terms": len(entries),
        "hits": result.hits,
        "ok": sum(1 for r in result.rows if r.status == "ok"),
        "missing": sum(1 for r in result.rows if r.status == "missing"),
        "total_rows": len(result.rows),
    }
    return result.rows, summary


def _termcheck_row_json(r) -> dict:
    return {
        "status": r.status,
        "block_pos": r.block_pos,
        "index": r.block_index,
        "start_tc": r.start_tc,
        "end_tc": r.end_tc,
        "en_line": r.en_line,
        "zh_line": r.zh_line,
        "term": r.term,
        "matched": r.matched,
        "expected_zh": r.expected_zh,
    }


@app.post("/api/termcheck")
def api_termcheck(req: TermCheckRequest):
    rows, summary = _build_termcheck_rows(req.srt, req.glossary)
    return {
        "summary": summary,
        "rows": [_termcheck_row_json(r) for r in rows],
    }


@app.post("/api/termcheck.csv")
def api_termcheck_csv(req: TermCheckRequest):
    rows, _ = _build_termcheck_rows(req.srt, req.glossary)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "状态", "块号", "声明序号", "时间码入点", "时间码出点",
            "英文轨原文", "中文轨原文", "英文术语", "命中文本", "应译词",
        ]
    )
    status_label = {"ok": "合格", "missing": "缺译"}
    for r in rows:
        writer.writerow(
            [
                status_label[r.status],
                r.block_pos,
                r.block_index,
                r.start_tc,
                r.end_tc,
                r.en_line,
                r.zh_line,
                r.term,
                r.matched,
                r.expected_zh,
            ]
        )
    # UTF-8 BOM，保证 Excel 直接打开中文不乱码
    data = "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="termcheck.csv"'
        },
    )


class DiffRequest(BaseModel):
    old_srt: str = Field(default="", description="旧版 SRT 字幕")
    new_srt: str = Field(default="", description="新版 SRT 字幕")


def _build_diff_rows(old_text: str, new_text: str):
    if len(old_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "旧版 SRT 超过 2 MB 大小限制")
    if len(new_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "新版 SRT 超过 2 MB 大小限制")
    try:
        old_subs = parse_srt(old_text)
    except ValueError as exc:
        raise HTTPException(400, f"旧版 SRT 解析失败：{exc}") from exc
    try:
        new_subs = parse_srt(new_text)
    except ValueError as exc:
        raise HTTPException(400, f"新版 SRT 解析失败：{exc}") from exc
    if not old_subs and not new_subs:
        raise HTTPException(400, "两侧内容均为空，请粘贴旧版与新版 SRT")
    if len(old_subs) > MAX_ITEMS or len(new_subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过每侧 {MAX_ITEMS} 块的上限，请拆分后再比较"
        )

    rows = []
    totals = {
        "unchanged": 0,
        "time_only": 0,
        "text_changed": 0,
        "split": 0,
        "merge": 0,
        "added": 0,
        "deleted": 0,
    }
    for op, cost, i, j in diff_align(old_subs, new_subs):
        if op == "pair":
            o, nn = old_subs[i], new_subs[j]
            if o.raw_text == nn.raw_text:
                status = (
                    "unchanged"
                    if o.start_ms == nn.start_ms and o.end_ms == nn.end_ms
                    else "time_only"
                )
            else:
                status = "text_changed"
            old_idx, new_idx = [i], [j]
        elif op == "split":
            status, old_idx, new_idx = "split", [i], [j, j + 1]
        elif op == "merge":
            status, old_idx, new_idx = "merge", [i, i + 1], [j]
        elif op == "gap_a":
            status, old_idx, new_idx = "deleted", [i], []
        else:
            status, old_idx, new_idx = "added", [], [j]

        old_items = [old_subs[k] for k in old_idx]
        new_items = [new_subs[k] for k in new_idx]
        rows.append(
            {
                "status": status,
                "cost": round(cost, 6),
                "old_index": [it.index for it in old_items],
                "new_index": [it.index for it in new_items],
                "old_start_tc": old_items[0].start_tc if old_items else "",
                "old_end_tc": old_items[-1].end_tc if old_items else "",
                "new_start_tc": new_items[0].start_tc if new_items else "",
                "new_end_tc": new_items[-1].end_tc if new_items else "",
                "old_text": "\n".join(it.raw_text for it in old_items),
                "new_text": "\n".join(it.raw_text for it in new_items),
                "time_dev_ms": time_dev_ms(old_subs, new_subs, op, i, j),
            }
        )
        totals[status] += 1

    summary = {
        **totals,
        "total_rows": len(rows),
        "total_cost": round(sum(r["cost"] for r in rows), 6),
        "old_blocks": len(old_subs),
        "new_blocks": len(new_subs),
    }
    return rows, summary


@app.post("/api/diff")
def api_diff(req: DiffRequest):
    rows, summary = _build_diff_rows(req.old_srt, req.new_srt)
    return {"summary": summary, "rows": rows}


@app.post("/api/diff.csv")
def api_diff_csv(req: DiffRequest):
    rows, _ = _build_diff_rows(req.old_srt, req.new_srt)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "状态", "代价",
            "旧版序号", "旧版入点", "旧版出点", "旧版正文",
            "新版序号", "新版入点", "新版出点", "新版正文",
            "入出点偏差(ms)",
        ]
    )
    status_label = {
        "unchanged": "未变",
        "time_only": "仅改时",
        "text_changed": "文字改动",
        "split": "拆分",
        "merge": "合并",
        "added": "新增",
        "deleted": "删除",
    }
    for r in rows:
        writer.writerow(
            [
                status_label[r["status"]],
                f"{r['cost']:.4f}",
                "\n".join(str(x) for x in r["old_index"]),
                r["old_start_tc"],
                r["old_end_tc"],
                r["old_text"],
                "\n".join(str(x) for x in r["new_index"]),
                r["new_start_tc"],
                r["new_end_tc"],
                r["new_text"],
                r["time_dev_ms"] if r["time_dev_ms"] is not None else "",
            ]
        )
    # UTF-8 BOM，保证 Excel 直接打开中文不乱码
    data = "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="diff.csv"'},
    )


class RebuildRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    clips: Any = Field(
        default_factory=list,
        description=(
            "剪辑数组，每项含非负整数毫秒 source_in、source_out、target_in，"
            "source_out 须大于 source_in；片段可乱序、重复，目标区间可重叠"
        ),
    )


def _build_rebuild_rows(srt_text: str, clips_in: list[dict]):
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再重建"
        )
    try:
        clips = validate_clips(clips_in)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if len(clips) > MAX_CLIPS:
        raise HTTPException(400, f"剪辑片段数超过 {MAX_CLIPS} 段的上限")
    return rebuild_subtitles(subs, clips)


def _rebuild_row_json(r) -> dict:
    return {
        "new_index": r.new_index,
        "start_tc": r.start_tc,
        "end_tc": r.end_tc,
        "source_indices": list(r.source_indices),
        "text": "\n".join(r.body_lines),
    }


@app.post("/api/rebuild")
def api_rebuild(req: RebuildRequest):
    rows = _build_rebuild_rows(req.srt, req.clips)
    block_count = len({i for r in rows for i in r.source_indices})
    return {
        "summary": {
            "total": len(rows),
            "clips": len(req.clips),
            "source_blocks": block_count,
        },
        "rows": [_rebuild_row_json(r) for r in rows],
    }


@app.post("/api/rebuild.srt")
def api_rebuild_srt(req: RebuildRequest):
    rows = _build_rebuild_rows(req.srt, req.clips)
    data = build_rebuild_srt(rows).encode("utf-8")
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="rebuilt.srt"'
        },
    )


class ReadingRequest(BaseModel):
    srt: str = Field(default="", description="原 SRT 字幕")
    speed: int = Field(
        default=10,
        ge=MIN_READING_SPEED,
        le=MAX_READING_SPEED,
        description="阅读速度（每秒 4~30 个显示宽度单位）",
    )


def _build_reading_rows(srt_text: str, speed: int):
    if not MIN_READING_SPEED <= speed <= MAX_READING_SPEED:
        raise HTTPException(
            400,
            f"阅读速度须在 {MIN_READING_SPEED}~{MAX_READING_SPEED} "
            "个显示宽度单位/秒之间",
        )
    if len(srt_text.encode("utf-8", errors="ignore")) > MAX_RAW_BYTES:
        raise HTTPException(400, "SRT 超过 2 MB 大小限制")
    try:
        subs = parse_srt(srt_text)
    except ValueError as exc:
        raise HTTPException(400, f"SRT 解析失败：{exc}") from exc
    if not subs:
        raise HTTPException(400, "SRT 内容为空，请粘贴字幕")
    if len(subs) > MAX_ITEMS:
        raise HTTPException(
            400, f"字幕块数超过 {MAX_ITEMS} 的上限，请拆分后再审查"
        )
    try:
        rows = review_reading_load(subs, speed)
    except ValueError as exc:
        raise HTTPException(400, f"阅读负载审查失败：{exc}") from exc
    # 预览与导出均按时间排列：入点升序，同入点按出点、解析序位
    rows.sort(key=lambda r: (r.start_ms, r.end_ms, r.pos))
    return rows


def _reading_row_json(r) -> dict:
    return {
        "status": r.status,
        "pos": r.pos,
        "index": r.index,
        "start_tc": r.start_tc,
        "end_tc": r.end_tc,
        "width": r.width,
        "required_ms": r.required_ms,
        "allocated_ms": r.allocated_ms,
        "unallocated_ms": r.unallocated_ms,
        "text": r.text,
    }


@app.post("/api/reading")
def api_reading(req: ReadingRequest):
    rows = _build_reading_rows(req.srt, req.speed)
    overdue = sum(1 for r in rows if r.status == "overdue")
    return {
        "summary": {
            "total": len(rows),
            "speed": req.speed,
            "ok": len(rows) - overdue,
            "overdue": overdue,
            "required_ms": sum(r.required_ms for r in rows),
            "allocated_ms": sum(r.allocated_ms for r in rows),
            "unallocated_ms": sum(r.unallocated_ms for r in rows),
        },
        "rows": [_reading_row_json(r) for r in rows],
    }


@app.post("/api/reading.csv")
def api_reading_csv(req: ReadingRequest):
    rows = _build_reading_rows(req.srt, req.speed)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "状态", "块号", "声明序号", "时间码入点", "时间码出点",
            "显示宽度", "所需毫秒", "已分配毫秒", "未分配毫秒", "正文",
        ]
    )
    status_label = {"ok": "合格", "overdue": "逾期"}
    for r in rows:
        writer.writerow(
            [
                status_label[r.status],
                r.pos,
                r.index,
                r.start_tc,
                r.end_tc,
                r.width,
                r.required_ms,
                r.allocated_ms,
                r.unallocated_ms,
                r.text,
            ]
        )
    # UTF-8 BOM，保证 Excel 直接打开中文不乱码
    data = "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="reading.csv"'
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
