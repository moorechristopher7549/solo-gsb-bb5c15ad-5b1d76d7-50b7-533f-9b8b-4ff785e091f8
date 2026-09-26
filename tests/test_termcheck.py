"""双语术语核对：词表解析、英文轨拼接、词边界匹配、重叠消解与中文核对测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.split import split_subtitles
from app.termcheck import (
    MAX_CHAIN_GAP_MS,
    check_terms,
    normalize_english,
    parse_glossary,
)

client = TestClient(app)


def _tc(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _srt(blocks, declared=None):
    """blocks: [(start_ms, end_ms, [正文行...]), ...]；declared 可指定声明序号。"""
    out = []
    for i, (s, e, lines) in enumerate(blocks, start=1):
        idx = declared[i - 1] if declared else i
        out.append(f"{idx}\n{_tc(s)} --> {_tc(e)}\n" + "\n".join(lines))
    return "\n\n".join(out) + "\n"


def _check(blocks, glossary, declared=None):
    items = parse_srt(_srt(blocks, declared))
    rows = split_subtitles(items)
    entries = parse_glossary(glossary)
    return check_terms(items, rows, entries)


# ---------- 英文归一化 ----------

def test_normalize_english_case_fullwidth_whitespace():
    assert normalize_english("  Machine   Learning ") == "machine learning"
    # 全角字母与全角空格 → 半角
    assert normalize_english("Ｍａｃｈｉｎｅ　Ｌｅａｒｎｉｎｇ") == "machine learning"
    assert normalize_english("A\tB\nC") == "a b c"


# ---------- 词表解析 ----------

def test_parse_glossary_basic_and_blank_lines():
    entries = parse_glossary(
        "\nmachine learning\t机器学习\n\ndeep learning\t深度 学习\n"
    )
    assert [e.order for e in entries] == [1, 2]
    assert [e.line_no for e in entries] == [2, 4]
    assert entries[0].en_raw == "machine learning"
    assert entries[0].en_norm == "machine learning"
    assert entries[0].zh_raw == "机器学习"
    assert entries[0].zh_key == "机器学习"
    # 指定译法内部空白在包含判定时去除
    assert entries[1].zh_key == "深度学习"


def test_parse_glossary_missing_tab_points_line():
    with pytest.raises(ValueError, match="词表第 2 行缺少制表符"):
        parse_glossary("machine learning\t机器学习\nmachine learning 机器学习\n")


def test_parse_glossary_empty_fields_point_line():
    with pytest.raises(ValueError, match="词表第 1 行英文术语为空"):
        parse_glossary("\t机器学习")
    with pytest.raises(ValueError, match="词表第 2 行指定中文译法为空"):
        parse_glossary("a b\t甲乙\nc d\t")


def test_parse_glossary_duplicate_after_normalization():
    # 大小写与空白归一后相同也算重复，并指出两处行号
    with pytest.raises(ValueError, match="词表第 2 行.*与第 1 行重复"):
        parse_glossary("Machine Learning\t机器学习\nmachine  learning\t机器之学\n")
    # 全角归一后与半角相同也算重复
    with pytest.raises(ValueError, match="重复"):
        parse_glossary("Ｍａｃｈｉｎｅ\t甲\nmachine\t乙\n")


# ---------- 英文轨拼接 ----------

def test_chains_join_only_gap_0_to_500():
    assert MAX_CHAIN_GAP_MS == 500
    blocks = [
        (0, 1000, ["甲。", "One."]),
        (1000, 2000, ["乙。", "Two."]),     # 间隔 0 → 拼接
        (2500, 3500, ["丙。", "Three."]),   # 间隔 500 → 拼接
        (4001, 5000, ["丁。", "Four."]),    # 间隔 501 → 断开
        (4500, 5500, ["戊。", "Five."]),    # 重叠（间隔为负）→ 断开
    ]
    result = _check(blocks, "zzz\t某")
    assert result.chains == 3
    assert result.hits == 0


def test_match_spanning_joined_blocks_checks_each_block():
    blocks = [
        (0, 1000, ["机器学习", "The machine"]),
        (1200, 2200, ["正在改变", "learning is fun"]),   # 间隔 200 → 拼接
    ]
    result = _check(blocks, "machine learning\t机器学习")
    assert result.chains == 1
    assert result.hits == 1
    # 一次命中覆盖两块：每块各记一条，分别核对
    assert [(r.block_pos, r.status) for r in result.rows] == [
        (1, "ok"),
        (2, "missing"),
    ]
    row = result.rows[1]
    assert row.en_line == "learning is fun"
    assert row.zh_line == "正在改变"
    assert row.term == "machine learning"
    assert row.expected_zh == "机器学习"


def test_no_match_across_broken_chain():
    blocks = [
        (0, 1000, ["机器学习", "The machine"]),
        (2000, 3000, ["正在改变", "learning is fun"]),   # 间隔 1000 > 500
    ]
    result = _check(blocks, "machine learning\t机器学习")
    assert result.chains == 2
    assert result.hits == 0
    assert result.rows == []


# ---------- 词边界 ----------

def test_word_boundary_rejects_substring():
    blocks = [(0, 2000, ["有人说过人工智能。", "He said AI will help."])]
    result = _check(blocks, "AI\t人工智能")
    # "said" 内的 "ai" 不匹配，独立的 "AI" 命中（归一化后为小写）
    assert result.hits == 1
    assert result.rows[0].matched == "ai"
    assert result.rows[0].status == "ok"


def test_word_boundary_rejects_digit_neighbour():
    blocks = [(0, 2000, ["文件。", "The mp4 file."])]
    result = _check(blocks, "mp\t某")
    assert result.hits == 0


def test_fullwidth_and_case_insensitive_match():
    blocks = [(0, 2000, ["机器学习。", "Ｍａｃｈｉｎｅ　ＬＥＡＲＮＩＮＧ。"])]
    result = _check(blocks, "machine learning\t机器学习")
    assert result.hits == 1
    assert result.rows[0].matched == "machine learning"
    assert result.rows[0].status == "ok"


# ---------- 重叠消解 ----------

def test_overlap_longest_wins_regardless_of_glossary_order():
    blocks = [(0, 2000, ["机器学习。", "Machine learning."])]
    for glossary in (
        "machine\t机器\nmachine learning\t机器学习\nlearning\t学习\n",
        "learning\t学习\nmachine\t机器\nmachine learning\t机器学习\n",
    ):
        result = _check(blocks, glossary)
        assert result.hits == 1
        assert result.rows[0].term == "machine learning"


def test_overlap_longer_wins_despite_later_start():
    blocks = [(0, 2000, ["去上学。", "Go to school."])]
    result = _check(blocks, "go to\t去\nto school\t上学")
    # "to school"（9 字符）比 "go to"（5 字符）覆盖更多，即使起点更靠后
    assert result.hits == 1
    assert result.rows[0].term == "to school"


def test_overlap_tie_by_position():
    blocks = [(0, 2000, ["甲乙丙。", "Aaa bbb ccc."])]
    result = _check(blocks, "bbb ccc\t乙丙\naaa bbb\t甲乙")
    # 等长候选重叠时先选出现位置靠前者（与词表顺序无关）
    assert result.hits == 1
    assert result.rows[0].term == "aaa bbb"


def test_same_term_multiple_occurrences_all_counted():
    blocks = [(0, 3000, ["人工智能与人工智能。", "AI and AI."])]
    result = _check(blocks, "ai\t人工智能")
    assert result.hits == 2
    assert [r.status for r in result.rows] == ["ok", "ok"]


# ---------- 中文核对 ----------

def test_zh_check_ignores_whitespace():
    blocks = [(0, 2000, ["机器 学习　改变 世界", "Machine learning."])]
    result = _check(blocks, "machine learning\t机器 学习\n")
    assert result.rows[0].status == "ok"


def test_missing_row_reports_block_pos_and_declared_index():
    blocks = [(0, 2000, ["改变了世界。", "Machine learning."])]
    result = _check(blocks, "machine learning\t机器学习", declared=[9])
    row = result.rows[0]
    assert row.status == "missing"
    assert row.block_pos == 1
    assert row.block_index == 9
    assert row.en_line == "Machine learning."
    assert row.expected_zh == "机器学习"


# ---------- 接口 ----------

SRT_BILINGUAL = _srt([
    (1000, 3000, ["机器学习正在改变世界。", "Machine learning is changing the world."]),
    (3200, 5000, ["神经网络是关键技术。", "The neural network is the key technology."]),
    (6000, 8000, ["深度学习需要大量数据。", "Deep learning needs lots of data."]),
])

GLOSSARY = (
    "machine learning\t机器学习\n"
    "neural network\t神经网络\n"
    "key technology\t核心技术\n"
    "deep learning\t深度学习\n"
)


def test_api_termcheck_preview():
    resp = client.post(
        "/api/termcheck", json={"srt": SRT_BILINGUAL, "glossary": GLOSSARY}
    )
    assert resp.status_code == 200
    data = resp.json()
    s = data["summary"]
    assert s["total_blocks"] == 3
    assert s["chains"] == 2          # 块1-2 间隔 200ms 拼接，块3 间隔 1000ms 断开
    assert s["terms"] == 4
    assert s["hits"] == 4
    assert s["ok"] == 3
    assert s["missing"] == 1
    assert s["total_rows"] == 4
    rows = data["rows"]
    assert [r["status"] for r in rows] == ["ok", "ok", "missing", "ok"]
    m = rows[2]
    assert m["block_pos"] == 2
    assert m["index"] == 2
    assert m["term"] == "key technology"
    assert m["expected_zh"] == "核心技术"
    assert m["en_line"] == "The neural network is the key technology."
    assert m["zh_line"] == "神经网络是关键技术。"
    assert m["start_tc"] == "00:00:03,200"
    assert m["end_tc"] == "00:00:05,000"


def test_api_termcheck_csv():
    resp = client.post(
        "/api/termcheck.csv", json={"srt": SRT_BILINGUAL, "glossary": GLOSSARY}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    raw = resp.content
    assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    assert "状态,块号,声明序号,时间码入点,时间码出点" in text
    assert "合格" in text and "缺译" in text
    assert "key technology" in text and "核心技术" in text
    # 4 条结果行（合格 3 + 缺译 1）
    assert len(text.strip().splitlines()) == 5


def test_api_termcheck_glossary_empty_item_400():
    for glossary in (
        "machine learning 机器学习\n",       # 缺 TAB
        "machine learning\t机器学习\n\t甲\n",  # 英文术语为空
        "machine learning\t\n",              # 指定译法为空
    ):
        resp = client.post(
            "/api/termcheck", json={"srt": SRT_BILINGUAL, "glossary": glossary}
        )
        assert resp.status_code == 400
        assert "词表第" in resp.json()["detail"]


def test_api_termcheck_duplicate_term_400():
    resp = client.post(
        "/api/termcheck",
        json={
            "srt": SRT_BILINGUAL,
            "glossary": "Machine Learning\t机器学习\nmachine learning\t机器之学\n",
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "第 2 行" in detail and "第 1 行" in detail and "重复" in detail


def test_api_termcheck_non_two_line_block_400():
    srt = _srt([
        (1000, 2000, ["你好。", "Hello."]),
        (2500, 3500, ["只有一行"]),
    ])
    for path in ("/api/termcheck", "/api/termcheck.csv"):
        resp = client.post(path, json={"srt": srt, "glossary": "hello\t你好"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "第 2 块" in detail and "恰好两行" in detail


def test_api_termcheck_empty_inputs_400():
    resp = client.post("/api/termcheck", json={"srt": "", "glossary": "a\t甲"})
    assert resp.status_code == 400
    resp = client.post(
        "/api/termcheck", json={"srt": SRT_BILINGUAL, "glossary": "  \n"}
    )
    assert resp.status_code == 400
    assert "词表" in resp.json()["detail"]
    resp = client.post(
        "/api/termcheck", json={"srt": "没有时间码", "glossary": "a\t甲"}
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]
