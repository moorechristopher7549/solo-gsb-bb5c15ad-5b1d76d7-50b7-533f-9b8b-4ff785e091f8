"""字幕阅读负载审查：显示宽度计量、所需时长取整、单时间轴分配模拟与接口测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.readload import (
    MAX_SPEED,
    MIN_SPEED,
    audit_reading_load,
    display_width,
    required_read_ms,
)

client = TestClient(app)


def _tc(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _srt(blocks, declared=None):
    """blocks: [(start_ms, end_ms, 正文), ...]；declared 可指定声明序号。"""
    out = []
    for i, (s, e, text) in enumerate(blocks, start=1):
        idx = declared[i - 1] if declared else i
        out.append(f"{idx}\n{_tc(s)} --> {_tc(e)}\n{text}")
    return "\n\n".join(out) + "\n"


def _audit(blocks, speed, declared=None):
    return audit_reading_load(parse_srt(_srt(blocks, declared)), speed)


# ---------- 显示宽度 ----------

def test_display_width_wide_two_narrow_one():
    assert display_width("你好世界") == 8
    assert display_width("Hello") == 5
    assert display_width("ＡＢc") == 5      # 全角计 2，半角计 1
    assert display_width("你好，世界！") == 12  # 全角标点同样计 2


def test_display_width_ignores_whitespace():
    assert display_width("你 好　 世界\n") == 8
    assert display_width(" a b\tc ") == 3


# ---------- 所需阅读毫秒数 ----------

def test_required_read_ms_rounds_up():
    assert required_read_ms(6, 10) == 600     # 整除不增
    assert required_read_ms(7, 30) == 234     # 233.33… 向上取整
    assert required_read_ms(1, 30) == 34
    assert required_read_ms(5, 4) == 1250


def test_speed_bounds():
    assert (MIN_SPEED, MAX_SPEED) == (4, 30)
    with pytest.raises(ValueError, match="阅读速度"):
        _audit([(0, 1000, "你好")], 3)
    with pytest.raises(ValueError, match="阅读速度"):
        _audit([(0, 1000, "你好")], 31)


# ---------- 入点不早于出点 ----------

def test_start_not_before_end_points_block():
    with pytest.raises(ValueError, match="第 2 块.*入点不早于出点"):
        _audit([
            (0, 1000, "正常块"),
            (2000, 2000, "零时长"),
        ], 10)
    # 声明序号与解析序位不一致时一并给出
    with pytest.raises(ValueError, match="第 1 块（声明序号 9）"):
        _audit([(3000, 1000, "出点更早")], 10, declared=[9])


# ---------- 单时间轴分配模拟 ----------

def test_non_overlapping_all_ok():
    rows = _audit([
        (0, 2000, "abcdef"),      # 宽 6 → 600ms
        (3000, 5000, "你好"),     # 宽 4 → 400ms
    ], 10)
    assert [r.status for r in rows] == ["ok", "ok"]
    assert [r.allocated_ms for r in rows] == [600, 400]
    assert [r.required_ms for r in rows] == [600, 400]
    assert all(r.unallocated_ms == 0 for r in rows)


def test_too_tight_block_overdue():
    rows = _audit([(0, 1000, "一二三四五六七八九十")], 10)   # 宽 20 → 2000ms
    (row,) = rows
    assert row.status == "overdue"
    assert row.required_ms == 2000
    assert row.allocated_ms == 1000      # 只能在自身 [0,1000) 内阅读
    assert row.unallocated_ms == 1000


def test_earliest_end_preempts_and_keeps_progress():
    rows = _audit([
        (0, 1000, "aaaaaa"),     # 600ms，出点 1000
        (400, 700, "bbbb"),      # 400ms，出点 700 更早 → 到达后抢占
    ], 10)
    a, b = rows
    # 块 2 在 [400,700) 内只读 300ms，逾期 100ms
    assert b.status == "overdue"
    assert b.allocated_ms == 300
    assert b.unallocated_ms == 100
    # 块 1 已读 400ms 进度保留，700 起续读 200ms 读完
    assert a.status == "ok"
    assert a.allocated_ms == 600


def test_same_end_tie_by_parse_order():
    rows = _audit([
        (0, 500, "aaa"),   # 300ms，同出点 → 解析序位靠前先读
        (0, 500, "bbb"),   # 300ms
    ], 10)
    first, second = rows
    assert first.status == "ok"
    assert first.allocated_ms == 300
    assert second.status == "overdue"   # 只在 [300,500) 读到 200ms
    assert second.allocated_ms == 200
    assert second.unallocated_ms == 100


def test_complete_exactly_at_end_is_ok():
    rows = _audit([(0, 1000, "aaaaaaaaaa")], 10)   # 恰好 1000ms 读完
    assert rows[0].status == "ok"
    assert rows[0].allocated_ms == 1000


def test_idle_gap_fast_forward():
    rows = _audit([
        (0, 1000, "aaaaa"),       # 500ms
        (5000, 6000, "bbbbb"),    # 500ms，中间空闲不影响
    ], 10)
    assert [r.status for r in rows] == ["ok", "ok"]
    assert [r.allocated_ms for r in rows] == [500, 500]


def test_rows_sorted_by_time_not_parse_order():
    rows = _audit([
        (5000, 6000, "晚到的块"),
        (0, 1000, "先播的块"),
    ], 10)
    assert [r.block_pos for r in rows] == [2, 1]
    assert [r.status for r in rows] == ["ok", "ok"]


# ---------- 接口 ----------

SRT_SAMPLE = _srt([
    (1000, 4000, "你好，世界！今天天气真不错。"),   # 宽 28 → 1867ms
    (4200, 5000, "这一块时间太短根本读不完。"),     # 宽 26 → 1734ms > 800ms
    (5000, 8000, "This line is easy to read."),   # 宽 21 → 1400ms
    (6000, 7500, "插入一块出点更早的"),            # 宽 18 → 1200ms，抢占块 3
])


def test_api_readload_preview():
    resp = client.post("/api/readload", json={"srt": SRT_SAMPLE, "speed": 15})
    assert resp.status_code == 200
    data = resp.json()
    s = data["summary"]
    assert s["total"] == 4
    assert s["speed"] == 15
    assert s["ok"] == 3
    assert s["overdue"] == 1
    assert s["unallocated_ms"] == 934
    assert s["required_ms"] == 1867 + 1734 + 1400 + 1200
    assert s["allocated_ms"] == s["required_ms"] - 934

    rows = data["rows"]
    assert [r["block_pos"] for r in rows] == [1, 2, 3, 4]
    assert [r["status"] for r in rows] == ["ok", "overdue", "ok", "ok"]
    overdue = rows[1]
    assert overdue["index"] == 2
    assert overdue["width"] == 26
    assert overdue["required_ms"] == 1734
    assert overdue["allocated_ms"] == 800
    assert overdue["unallocated_ms"] == 934
    assert overdue["start_tc"] == "00:00:04,200"
    assert overdue["end_tc"] == "00:00:05,000"
    # 块 3 被出点更早的块 4 抢占后仍在自身区间内读完
    assert rows[2]["allocated_ms"] == 1400
    assert rows[3]["allocated_ms"] == 1200


def test_api_readload_default_speed():
    resp = client.post("/api/readload", json={"srt": SRT_SAMPLE})
    assert resp.status_code == 200
    assert resp.json()["summary"]["speed"] == 15


def test_api_readload_csv():
    resp = client.post("/api/readload.csv", json={"srt": SRT_SAMPLE, "speed": 15})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    raw = resp.content
    assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    assert "状态,块号,声明序号,时间码入点,时间码出点" in text
    assert "显示宽度,所需毫秒,已分配毫秒,未分配毫秒" in text
    assert "合格" in text and "逾期" in text
    # 表头 + 4 条结果行
    assert len(text.strip().splitlines()) == 5


def test_api_readload_bad_speed():
    for bad in (0, 3, 31, 100):
        resp = client.post(
            "/api/readload", json={"srt": SRT_SAMPLE, "speed": bad}
        )
        assert resp.status_code in (400, 422)


def test_api_readload_empty_and_invalid_srt():
    resp = client.post("/api/readload", json={"srt": "", "speed": 15})
    assert resp.status_code == 400
    resp = client.post("/api/readload", json={"srt": "没有时间码", "speed": 15})
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]


def test_api_readload_start_not_before_end_400():
    srt = _srt([(0, 1000, "正常块"), (2000, 1500, "出点更早")])
    for path in ("/api/readload", "/api/readload.csv"):
        resp = client.post(path, json={"srt": srt, "speed": 15})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "第 2 块" in detail and "入点不早于出点" in detail
