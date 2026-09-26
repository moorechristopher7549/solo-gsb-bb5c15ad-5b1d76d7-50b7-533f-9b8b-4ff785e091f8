"""字幕阅读负载审查：显示宽度/所需时长/EDF 分配算法单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.reading import (
    display_width,
    required_reading_ms,
    review_reading_load,
)

client = TestClient(app)


# ---------- 宽度计量与所需时长 ----------

def test_display_width_removes_whitespace():
    # 空白（含全角空格、Tab、换行）全部不计；宽字 2、窄字 1
    assert display_width("你好 world") == 2 * 2 + 5
    assert display_width(" 你\t好\n") == 4
    assert display_width("Ａ") == 2          # 全角拉丁为宽字符
    assert display_width("，！？") == 6      # 全角标点为宽字符
    assert display_width("abc!") == 4
    assert display_width(" \t　\n") == 0


def test_required_reading_ms_rounds_up():
    assert required_reading_ms(10, 10) == 1000
    # 3 个单位 / 每秒 10 个 = 300ms，整除
    assert required_reading_ms(3, 10) == 300
    # 1 个单位 / 每秒 30 个 = 33.33ms，向上取整
    assert required_reading_ms(1, 30) == 34
    # 4 个单位 / 每秒 3 个... 取常见速度：7 单位 / 每秒 4 = 1750ms，整除
    assert required_reading_ms(7, 4) == 1750
    assert required_reading_ms(0, 10) == 0


# ---------- 单块 ----------

def test_single_block_fits():
    items = parse_srt("1\n00:00:00,000 --> 00:00:01,000\n一二三四五\n")
    rows = review_reading_load(items, 10)
    assert len(rows) == 1
    r = rows[0]
    assert r.width == 10 and r.required_ms == 1000
    assert r.allocated_ms == 1000 and r.unallocated_ms == 0
    assert r.status == "ok"


def test_single_block_overdue():
    items = parse_srt("1\n00:00:00,000 --> 00:00:01,000\n一二三四五六\n")
    rows = review_reading_load(items, 10)
    r = rows[0]
    assert r.required_ms == 1200
    assert r.allocated_ms == 1000          # 只能在 [0,1000) 内读
    assert r.unallocated_ms == 200
    assert r.status == "overdue"


def test_zero_length_block_raises_and_points_position():
    items = parse_srt(
        "1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
        "9\n00:00:02,000 --> 00:00:02,000\n乙\n"
    )
    with pytest.raises(ValueError, match="第 2 块"):
        review_reading_load(items, 10)


def test_start_after_end_raises_with_declared_index():
    # 解析序位与声明序号不一致时一并给出声明序号
    items = parse_srt("1\n00:00:03,000 --> 00:00:01,000\n倒着走\n")
    with pytest.raises(ValueError, match="第 1 块"):
        review_reading_load(items, 10)


# ---------- 最早出点优先（EDF）----------

def test_edf_preemption_earliest_end_first():
    # 0 秒同时放行两块：块 1 出点 2000、块 2 出点 1000
    items = parse_srt(
        "1\n00:00:00,000 --> 00:00:02,000\n一二三四五六七八九十\n\n"
        "2\n00:00:00,000 --> 00:00:01,000\n短块\n"
    )
    rows = review_reading_load(items, 10)
    by_pos = {r.pos: r for r in rows}
    # 块 2 需 400ms、出点最早，先读完
    assert by_pos[2].required_ms == 400
    assert by_pos[2].allocated_ms == 400
    assert by_pos[2].status == "ok"
    # 块 1 在 [400,2000) 得到 1600ms，需 2000ms，逾期 400ms
    assert by_pos[1].allocated_ms == 1600
    assert by_pos[1].unallocated_ms == 400
    assert by_pos[1].status == "overdue"


def test_tie_same_end_parse_order_first():
    # 两块入点、出点完全相同：同出点按解析序位，块 1 先读
    items = parse_srt(
        "1\n00:00:00,000 --> 00:00:01,000\n一\n\n"
        "2\n00:00:00,000 --> 00:00:01,000\n二三四五六七八九十\n"
    )
    rows = review_reading_load(items, 10)
    by_pos = {r.pos: r for r in rows}
    assert by_pos[1].allocated_ms == 200 and by_pos[1].status == "ok"
    # 块 2 在 [200,1000) 得到 800ms，需 1800ms
    assert by_pos[2].allocated_ms == 800
    assert by_pos[2].unallocated_ms == 1000
    assert by_pos[2].status == "overdue"


def test_non_overlapping_blocks_each_own_window():
    items = parse_srt(
        "1\n00:00:00,000 --> 00:00:01,000\n一二三四五\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n一二三四\n"
    )
    rows = review_reading_load(items, 10)
    # 时间轴空闲时跳到下一个入点，各块都在自身窗口内读完
    assert [(r.pos, r.required_ms, r.allocated_ms, r.status) for r in rows] == [
        (1, 1000, 1000, "ok"),
        (2, 800, 800, "ok"),
    ]


def test_released_later_with_earlier_end_preempts():
    # 块 1：[0,2000) 需 2000ms；块 2：[500,1000) 需 500ms，后放行但出点更早
    items = parse_srt(
        "1\n00:00:00,000 --> 00:00:02,000\n一二三四五六七八九十\n\n"
        "2\n00:00:00,500 --> 00:00:01,000\n短块\n"
    )
    rows = review_reading_load(items, 10)
    by_pos = {r.pos: r for r in rows}
    # 块 2：[500,1000) 读满 500ms 读完（需 400ms）
    assert by_pos[2].allocated_ms == 400 and by_pos[2].status == "ok"
    # 块 1：[0,500) + [900,2000) = 500 + 1100 = 1600ms，逾期 400ms
    assert by_pos[1].allocated_ms == 1600
    assert by_pos[1].unallocated_ms == 400


def test_block_finished_at_exact_end_is_ok():
    # 左闭右开：恰好在出点读完仍算合格（10 宽 / 每秒 10 = 1000ms 用满窗口）
    items = parse_srt("1\n00:00:00,000 --> 00:00:01,000\n一二三四五\n")
    rows = review_reading_load(items, 10)
    assert rows[0].allocated_ms == 1000
    assert rows[0].status == "ok"


# ---------- 接口 ----------

SRT = "\n".join(
    [
        "1",
        "00:00:00,000 --> 00:00:02,000",
        "一二三四五六七八九十",
        "",
        "2",
        "00:00:00,000 --> 00:00:01,000",
        "短块",
        "",
        "3",
        "00:00:02,000 --> 00:00:03,000",
        "时间充足",
        "",
    ]
)


def test_api_reading_preview_sorted_by_time():
    resp = client.post("/api/reading", json={"srt": SRT, "speed": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"] == {
        "total": 3,
        "speed": 10,
        "ok": 2,
        "overdue": 1,
        "required_ms": 2000 + 400 + 800,
        "allocated_ms": 1600 + 400 + 800,
        "unallocated_ms": 400,
    }
    rows = data["rows"]
    # 按时间排列：块 2 与块 1 同入点，出点更早的块 2 在前
    assert [(r["pos"], r["status"]) for r in rows] == [
        (2, "ok"), (1, "overdue"), (3, "ok")
    ]
    overdue = next(r for r in rows if r["status"] == "overdue")
    assert overdue["required_ms"] == 2000
    assert overdue["allocated_ms"] == 1600
    assert overdue["unallocated_ms"] == 400
    assert overdue["width"] == 20
    assert overdue["start_tc"] == "00:00:00,000"


def test_api_reading_csv_download():
    resp = client.post("/api/reading.csv", json={"srt": SRT, "speed": 10})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="reading.csv"' in resp.headers["content-disposition"]
    raw = resp.content
    assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == (
        "状态,块号,声明序号,时间码入点,时间码出点,显示宽度,"
        "所需毫秒,已分配毫秒,未分配毫秒,正文"
    )
    assert "逾期,1,1," in text and "合格,2,2," in text
    assert "00:00:00,000" in text and "一二三四五六七八九十" in text


def test_api_reading_speed_boundaries_accepted():
    for speed in (4, 30):
        resp = client.post("/api/reading", json={"srt": SRT, "speed": speed})
        assert resp.status_code == 200
        assert resp.json()["summary"]["speed"] == speed


def test_api_reading_speed_out_of_range_rejected():
    for speed in (3, 0, 31, 100):
        resp = client.post("/api/reading", json={"srt": SRT, "speed": speed})
        assert resp.status_code in (400, 422)


def test_api_reading_non_integer_speed_422():
    resp = client.post("/api/reading", json={"srt": SRT, "speed": 10.5})
    assert resp.status_code == 422


def test_api_reading_zero_length_block_400_points_block():
    bad_srt = "\n".join(
        [
            "1",
            "00:00:00,000 --> 00:00:01,000",
            "正常块",
            "",
            "2",
            "00:00:02,000 --> 00:00:02,000",
            "零时长",
            "",
        ]
    )
    resp = client.post("/api/reading", json={"srt": bad_srt, "speed": 10})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "第 2 块" in detail and "入点不早于出点" in detail


def test_api_reading_empty_srt_400():
    resp = client.post("/api/reading", json={"srt": "", "speed": 10})
    assert resp.status_code == 400


def test_api_reading_bad_srt_400():
    resp = client.post(
        "/api/reading", json={"srt": "没有时间码", "speed": 10}
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]


def test_api_reading_csv_rejects_zero_length_block():
    bad_srt = "1\n00:00:02,000 --> 00:00:01,000\n倒着走\n"
    resp = client.post("/api/reading.csv", json={"srt": bad_srt, "speed": 10})
    assert resp.status_code == 400
    assert "第 1 块" in resp.json()["detail"]
