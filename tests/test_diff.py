"""SRT 版本差异审校：DP 单元测试（含暴力枚举对拍、numpy/纯 Python 一致性）+ 接口测试。

运行：python -m pytest tests/ -q
"""
import random

import pytest
from fastapi.testclient import TestClient

from app.diffalign import (
    GAP_COST,
    SPLIT_EXTRA,
    _HAVE_NUMPY,
    _align_python,
    diff_align,
    time_dev_ms,
)
from app.main import app
from app.parsers import SrtItem, format_tc
from app.textnorm import normalize_text, normalized_distance

client = TestClient(app)


def _item(idx, text, start_ms, end_ms):
    return SrtItem(
        index=idx,
        raw_text=text,
        start_ms=start_ms,
        end_ms=end_ms,
        start_tc=format_tc(start_ms),
        end_tc=format_tc(end_ms),
    )


def _seq(texts, start=0, step=1000, dur=800):
    items = []
    t = start
    for k, text in enumerate(texts, 1):
        items.append(_item(k, text, t, t + dur))
        t += step
    return items


def _ops_key(old, new, ops):
    """操作序列的 (总分, 入出点偏差, 拆并次数) 键。"""
    p = sum(c for _, c, _, _ in ops)
    t = sum(time_dev_ms(old, new, op, i, j) or 0 for op, _, i, j in ops)
    k = sum(1 for op, *_ in ops if op in ("split", "merge"))
    return p, t, k


def _assert_cover(old, new, ops):
    """操作序列须按块序完整消费两侧条目：全局单调、每块恰好一次。"""
    ai = bj = 0
    for op, _, i, j in ops:
        if op in ("pair", "split", "merge", "gap_a"):
            assert i == ai
        else:
            assert i is None
        if op in ("pair", "split", "merge", "gap_b"):
            assert j == bj
        else:
            assert j is None
        ai += {"pair": 1, "split": 1, "merge": 2, "gap_a": 1, "gap_b": 0}[op]
        bj += {"pair": 1, "split": 2, "merge": 1, "gap_a": 0, "gap_b": 1}[op]
    assert ai == len(old)
    assert bj == len(new)


# ---------- 计分常量与基本操作 ----------

def test_cost_constants():
    assert GAP_COST == 0.45      # 增删各计 0.45
    assert SPLIT_EXTRA == 0.1    # 拆并另加 0.1


def test_identical_sequences_all_pairs():
    old = _seq(["你好，世界！", "今天天气真好", "再见！"])
    new = _seq(["你好，世界！", "今天天气真好", "再见！"])
    ops = diff_align(old, new)
    assert [op for op, *_ in ops] == ["pair", "pair", "pair"]
    assert all(c == 0.0 for _, c, _, _ in ops)
    assert _ops_key(old, new, ops) == (0.0, 0, 0)


def test_added_and_deleted_cost():
    old = _seq(["甲"])
    new = _seq(["甲", "乙", "丙"])
    ops = diff_align(old, new)
    assert [op for op, *_ in ops] == ["pair", "gap_b", "gap_b"]
    assert [c for _, c, _, _ in ops] == [0.0, GAP_COST, GAP_COST]

    ops = diff_align(new, old)
    assert [op for op, *_ in ops] == ["pair", "gap_a", "gap_a"]
    assert [c for _, c, _, _ in ops] == [0.0, GAP_COST, GAP_COST]


def test_empty_side_shortcut():
    new = _seq(["甲", "乙"])
    assert diff_align([], new) == [
        ("gap_b", GAP_COST, None, 0),
        ("gap_b", GAP_COST, None, 1),
    ]
    assert diff_align(new, []) == [
        ("gap_a", GAP_COST, 0, None),
        ("gap_a", GAP_COST, 1, None),
    ]


def test_split_cost_is_text_distance_plus_extra():
    # 合并后正文归一化相同，文本分 0，代价恰为另加的 0.1
    old = _seq(["abcdef"])
    new = _seq(["abc", "def"])
    ops = diff_align(old, new)
    assert [op for op, *_ in ops] == ["split"]
    assert ops[0][1] == pytest.approx(SPLIT_EXTRA)
    assert ops[0][2] == 0 and ops[0][3] == 0


def test_merge_cost_is_text_distance_plus_extra():
    old = _seq(["abc", "def"])
    new = _seq(["abcdef"])
    ops = diff_align(old, new)
    assert [op for op, *_ in ops] == ["merge"]
    assert ops[0][1] == pytest.approx(SPLIT_EXTRA)


def test_split_preferred_over_pair_plus_gap():
    # 拆成两句的长句：split（文本分 + 0.1）应优于 1:1 + 新增（≈0.53 + 0.45）
    old = _seq(["这一句在新版里被拆成了两句短句。"])
    new = _seq(["这一句在新版里被拆开了。", "拆成了两句短句。"])
    ops = diff_align(old, new)
    assert [op for op, *_ in ops] == ["split"]
    assert ops[0][1] < GAP_COST


# ---------- 平局规则：先入出点偏差，再 1:1 ----------

def test_tie_prefers_smaller_time_dev():
    # 两个新块正文相同：1:1 配哪个总分都一样，应配入出点偏差更小者
    old = [_item(1, "你好", 1000, 2000)]
    new = [_item(1, "你好", 900, 1900), _item(2, "你好", 1500, 2500)]
    ops = diff_align(old, new)
    assert ops == [("pair", 0.0, 0, 0), ("gap_b", GAP_COST, None, 1)]


def test_tie_prefers_pair_over_split():
    # pair 文本分 11/110=0.1、split 文本分 81/180=0.45：
    # 0.1+0.45 与 0.45+0.1 浮点恒等（IEEE 加法交换律），总分相同；
    # 再令两侧入出点偏差相同（新两块出点相同），此时须优先 1:1
    assert 11 / 110 + GAP_COST == 81 / 180 + SPLIT_EXTRA
    old = [_item(1, "a" * 110, 0, 1000)]
    new = [_item(1, "a" * 99, 0, 1000), _item(2, "c" * 81, 500, 1000)]
    ops = diff_align(old, new)
    assert [op for op, *_ in ops] == ["pair", "gap_b"]
    assert _ops_key(old, new, ops)[0] == pytest.approx(0.1 + GAP_COST)


# ---------- 暴力枚举对拍 + numpy/纯 Python 一致性 ----------

def _brute_best_key(old, new):
    """暴力枚举全部对齐，返回最优 (总分, 入出点偏差, 拆并次数)。"""
    on1 = [normalize_text(it.raw_text) for it in old]
    on2 = [
        normalize_text(old[i].raw_text + " " + old[i + 1].raw_text)
        for i in range(len(old) - 1)
    ]
    nn1 = [normalize_text(it.raw_text) for it in new]
    nn2 = [
        normalize_text(new[j].raw_text + " " + new[j + 1].raw_text)
        for j in range(len(new) - 1)
    ]
    best = None

    def rec(i, j, key):
        nonlocal best
        if best is not None and key >= best:  # 各分量只增不减，可剪枝
            return
        if i == len(old) and j == len(new):
            best = key
            return
        if i < len(old) and j < len(new):
            rec(i + 1, j + 1, (
                key[0] + normalized_distance(on1[i], nn1[j]),
                key[1] + time_dev_ms(old, new, "pair", i, j),
                key[2],
            ))
        if i < len(old) and j + 1 < len(new):
            rec(i + 1, j + 2, (
                key[0] + normalized_distance(on1[i], nn2[j]) + SPLIT_EXTRA,
                key[1] + time_dev_ms(old, new, "split", i, j),
                key[2] + 1,
            ))
        if i + 1 < len(old) and j < len(new):
            rec(i + 2, j + 1, (
                key[0] + normalized_distance(on2[i], nn1[j]) + SPLIT_EXTRA,
                key[1] + time_dev_ms(old, new, "merge", i, j),
                key[2] + 1,
            ))
        if i < len(old):
            rec(i + 1, j, (key[0] + GAP_COST, key[1], key[2]))
        if j < len(new):
            rec(i, j + 1, (key[0] + GAP_COST, key[1], key[2]))

    rec(0, 0, (0.0, 0, 0))
    return best


def _random_seq(rng, k):
    items = []
    t = 0
    for idx in range(1, k + 1):
        text = "".join(
            rng.choice("aabb你你好好，。") for _ in range(rng.randint(1, 8))
        )
        dur = rng.choice([400, 700, 1000])
        items.append(_item(idx, text, t, t + dur))
        t += rng.choice([0, 300, 800, 1200])
    return items


def test_bruteforce_random_small():
    rng = random.Random(20260925)
    for _ in range(120):
        old = _random_seq(rng, rng.randint(1, 4))
        new = _random_seq(rng, rng.randint(1, 4))
        ops = diff_align(old, new)
        _assert_cover(old, new, ops)
        p, t, k = _ops_key(old, new, ops)
        bp, bt, bk = _brute_best_key(old, new)
        assert p == pytest.approx(bp, abs=1e-9)
        assert (t, k) == (bt, bk)


@pytest.mark.skipif(not _HAVE_NUMPY, reason="需要 numpy")
def test_python_fallback_matches_numpy():
    rng = random.Random(7)
    for _ in range(60):
        old = _random_seq(rng, rng.randint(1, 5))
        new = _random_seq(rng, rng.randint(1, 5))
        kn = _ops_key(old, new, diff_align(old, new))
        kp = _ops_key(old, new, _align_python(old, new))
        assert kn[0] == pytest.approx(kp[0], abs=1e-9)
        assert kn[1:] == kp[1:]


# ---------- 接口测试 ----------

OLD_SRT = "\n".join(
    [
        "1",
        "00:00:01,000 --> 00:00:02,500",
        "你好，世界！",
        "",
        "2",
        "00:00:03,000 --> 00:00:04,500",
        "今天我们来聊聊字幕校对。",
        "",
        "3",
        "00:00:05,000 --> 00:00:06,000",
        "这句稍后会被删掉。",
        "",
        "4",
        "00:00:06,500 --> 00:00:08,000",
        "这一句在新版里被拆成了两句短句。",
        "",
        "5",
        "00:00:08,500 --> 00:00:09,000",
        "前一半。",
        "",
        "6",
        "00:00:09,000 --> 00:00:09,500",
        "后一半。",
        "",
        "7",
        "00:00:10,000 --> 00:00:11,000",
        "再见！",
        "",
    ]
)

NEW_SRT = "\n".join(
    [
        "1",
        "00:00:01,000 --> 00:00:02,500",
        "你好，世界！",
        "",
        "2",
        "00:00:03,200 --> 00:00:04,700",
        "今天我们来聊聊字幕校对。",
        "",
        "3",
        "00:00:04,800 --> 00:00:05,200",
        "This line is brand new.",
        "",
        "4",
        "00:00:06,500 --> 00:00:07,000",
        "这一句在新版里被拆开了。",
        "",
        "5",
        "00:00:07,000 --> 00:00:08,000",
        "拆成了两句短句。",
        "",
        "6",
        "00:00:08,500 --> 00:00:09,500",
        "前一半。后一半。",
        "",
        "7",
        "00:00:10,000 --> 00:00:11,000",
        "再会！",
        "",
    ]
)


def _diff():
    resp = client.post("/api/diff", json={"old_srt": OLD_SRT, "new_srt": NEW_SRT})
    assert resp.status_code == 200
    return resp.json()


def test_diff_sample_statuses():
    data = _diff()
    s = data["summary"]
    assert s["unchanged"] == 1
    assert s["time_only"] == 1
    assert s["text_changed"] == 1
    assert s["split"] == 1
    assert s["merge"] == 1
    assert s["added"] == 1
    assert s["deleted"] == 1
    assert s["total_rows"] == 7
    assert s["old_blocks"] == 7
    assert s["new_blocks"] == 7

    by_status = {}
    for r in data["rows"]:
        by_status.setdefault(r["status"], []).append(r)

    assert by_status["unchanged"][0]["old_index"] == [1]
    assert by_status["unchanged"][0]["new_index"] == [1]

    tm = by_status["time_only"][0]
    assert tm["old_index"] == [2] and tm["new_index"] == [2]
    assert tm["cost"] == 0.0
    assert tm["old_start_tc"] == "00:00:03,000"
    assert tm["new_start_tc"] == "00:00:03,200"
    assert tm["time_dev_ms"] == 400

    assert by_status["deleted"][0]["old_index"] == [3]
    assert by_status["deleted"][0]["new_index"] == []
    assert by_status["added"][0]["old_index"] == []
    assert by_status["added"][0]["new_index"] == [3]
    # 增删各计 0.45
    assert by_status["added"][0]["cost"] == pytest.approx(0.45)
    assert by_status["deleted"][0]["cost"] == pytest.approx(0.45)

    sp = by_status["split"][0]
    assert sp["old_index"] == [4] and sp["new_index"] == [4, 5]
    assert sp["old_text"] == "这一句在新版里被拆成了两句短句。"
    assert sp["new_text"] == "这一句在新版里被拆开了。\n拆成了两句短句。"
    assert sp["new_start_tc"] == "00:00:06,500"
    assert sp["new_end_tc"] == "00:00:08,000"

    mg = by_status["merge"][0]
    assert mg["old_index"] == [5, 6] and mg["new_index"] == [6]
    assert mg["old_text"] == "前一半。\n后一半。"
    assert mg["new_text"] == "前一半。后一半。"
    # 合并后正文归一化相同，文本分 0，代价恰为另加的 0.1
    assert mg["cost"] == pytest.approx(0.1)

    tc = by_status["text_changed"][0]
    assert tc["old_index"] == [7] and tc["new_index"] == [7]
    assert tc["old_text"] == "再见！" and tc["new_text"] == "再会！"


def test_diff_rows_cover_each_block_once():
    data = _diff()
    seen_old, seen_new = [], []
    for r in data["rows"]:
        seen_old.extend(r["old_index"])
        seen_new.extend(r["new_index"])
    assert seen_old == [1, 2, 3, 4, 5, 6, 7]
    assert seen_new == [1, 2, 3, 4, 5, 6, 7]


def test_diff_bad_old_srt_400():
    resp = client.post("/api/diff", json={"old_srt": "没有时间码", "new_srt": NEW_SRT})
    assert resp.status_code == 400
    assert "旧版" in resp.json()["detail"]


def test_diff_bad_new_srt_400():
    resp = client.post("/api/diff", json={"old_srt": OLD_SRT, "new_srt": "没有时间码"})
    assert resp.status_code == 400
    assert "新版" in resp.json()["detail"]


def test_diff_both_empty_400():
    resp = client.post("/api/diff", json={"old_srt": "  \n", "new_srt": ""})
    assert resp.status_code == 400


def test_diff_one_side_empty():
    resp = client.post("/api/diff", json={"old_srt": "", "new_srt": NEW_SRT})
    assert resp.status_code == 200
    data = resp.json()
    assert all(r["status"] == "added" for r in data["rows"])
    assert data["summary"]["added"] == 7

    resp = client.post("/api/diff", json={"old_srt": OLD_SRT, "new_srt": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert all(r["status"] == "deleted" for r in data["rows"])
    assert data["summary"]["deleted"] == 7


def test_diff_csv_export():
    resp = client.post("/api/diff.csv", json={"old_srt": OLD_SRT, "new_srt": NEW_SRT})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "diff.csv" in resp.headers["content-disposition"]
    raw = resp.content
    assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header == (
        "状态,代价,旧版序号,旧版入点,旧版出点,旧版正文,"
        "新版序号,新版入点,新版出点,新版正文,入出点偏差(ms)"
    )
    for label in ("未变", "仅改时", "文字改动", "拆分", "合并", "新增", "删除"):
        assert label in text
    assert "00:00:01,000" in text
