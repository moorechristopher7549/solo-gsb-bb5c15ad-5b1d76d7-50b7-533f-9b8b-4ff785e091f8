"""双语 SRT 拆轨：计分与全段最优 DP 单元测试（含暴力枚举对拍）+ 接口测试。

运行：python -m pytest tests/ -q
"""
import itertools
import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.split import (
    EN_TOP,
    SWITCH_COST,
    ZH_TOP,
    _best_orders,
    _is_hanzi,
    _is_latin_letter,
    build_split_srt,
    split_subtitles,
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


def _split(blocks, declared=None):
    return split_subtitles(parse_srt(_srt(blocks, declared)))


# ---------- 字符判定 ----------

def test_hanzi_classification():
    for ch in "中文字一二三":
        assert _is_hanzi(ch)
    assert _is_hanzi("〇")          # 表意数字零
    assert _is_hanzi("㐂")          # 扩展 A 区
    for ch in "aZ1，。！… ":
        assert not _is_hanzi(ch)


def test_latin_letter_classification():
    for ch in "aAzZ":
        assert _is_latin_letter(ch)
    assert _is_latin_letter("é")    # 带附加符号的拉丁字母
    assert _is_latin_letter("Ａ")   # 全角拉丁字母
    for ch in "αΩДж中1，✝":        # 希腊/西里尔/汉字/数字/标点/符号均不算
        assert not _is_latin_letter(ch)


# ---------- 基本归属与错配计分 ----------

def test_zh_top_and_en_top_detection():
    rows = _split([
        (1000, 3000, ["你好，世界！", "Hello, world!"]),   # 中文在上
        (3500, 5500, ["Splitting is easy.", "拆轨简单。"]),  # 英文在上
    ])
    assert [r.order for r in rows] == [ZH_TOP, EN_TOP]
    assert rows[0].zh_line == "你好，世界！"
    assert rows[0].en_line == "Hello, world!"
    assert rows[1].zh_line == "拆轨简单。"
    assert rows[1].en_line == "Splitting is easy."
    assert [r.mismatch for r in rows] == [0, 0]
    assert [r.switched for r in rows] == [False, True]


def test_mismatch_counts_per_character():
    # 上行 2 汉字 + 3 拉丁字母，下行 2 拉丁字母 + 2 汉字：
    # 中文在上 = 3 + 2 = 5；英文在上 = 2 + 2 = 4 → 取英文在上
    rows = _split([(1000, 2000, ["你好abc", "de世界"])])
    assert rows[0].order == EN_TOP
    assert rows[0].mismatch == 4
    assert rows[0].zh_line == "de世界"
    assert rows[0].en_line == "你好abc"


def test_digits_and_punct_are_neutral():
    # 数字、标点、空白不计错配：纯中性块两种归属代价均为 0
    rows = _split([(1000, 2000, ["…… 123", "??? 456"])])
    assert rows[0].mismatch == 0
    assert rows[0].order == ZH_TOP   # 单块平局：中文在上


def test_tie_single_block_prefers_zh_top():
    rows = _split([(1000, 2000, ["1", "2"])])
    assert [r.order for r in rows] == [ZH_TOP]


def test_neutral_block_follows_agreeing_neighbors():
    # 中性块夹在两个英文在上块之间：跟随邻块（顺序变化 4 分 > 0 错配）
    rows = _split([
        (1000, 2000, ["Hello there.", "你好。"]),
        (2500, 3500, ["……", "……"]),
        (4000, 5000, ["See you.", "再见。"]),
    ])
    assert [r.order for r in rows] == [EN_TOP, EN_TOP, EN_TOP]
    assert not any(r.switched for r in rows)


def test_global_optimum_beats_per_block_greedy():
    # 第 2 块单独看英文在上错配更少（0 < 3），但翻转引入两次顺序变化
    # 计 4 分；全段最优让其中文在上（总代价 3 < 4），逐块贪心会翻错
    rows = _split([
        (1000, 2000, ["你好世界。", "Hello world."]),
        (2500, 3500, ["ab", "好"]),      # 中文在上 2+1=3；英文在上 0
        (4000, 5000, ["今天天气好。", "Nice day."]),
    ])
    assert [r.order for r in rows] == [ZH_TOP, ZH_TOP, ZH_TOP]
    assert [r.mismatch for r in rows] == [0, 3, 0]
    assert not any(r.switched for r in rows)


def test_switch_cost_makes_isolated_flip_unworthy():
    # 中间块单独翻转可省 1 错配（3→2），但两次顺序变化计 4 分，不划算
    rows = _split([
        (1000, 2000, ["你好。", "Hello."]),
        (2500, 3500, ["ab", "cd好"]),    # 中文在上 2+1=3；英文在上 0+2=2
        (4000, 5000, ["世界。", "World."]),
    ])
    assert [r.order for r in rows] == [ZH_TOP, ZH_TOP, ZH_TOP]


def test_switch_cost_allows_worthy_flip():
    # 中间块翻转可省 10 错配 > 4 分顺序变化，应当翻转
    rows = _split([
        (1000, 2000, ["你好。", "Hi."]),
        (2500, 3500, ["abcdefghij", "甲乙丙丁戊"]),
        (4000, 5000, ["世界。", "World."]),
    ])
    assert [r.order for r in rows] == [ZH_TOP, EN_TOP, ZH_TOP]
    assert [r.switched for r in rows] == [False, True, True]


# ---------- 暴力枚举对拍：全段最优性 + 平局逐块中文在上 ----------

def _brute_best(zh_costs, en_costs):
    """枚举全部归属方案，返回 (最小总代价, 对应轨序元组)。

    平局按轨序元组字典序取优（0=中文在上 < 1=英文在上），
    即从第 1 块起逐块优先中文在上。
    """
    n = len(zh_costs)
    best = None
    for bits in itertools.product((0, 1), repeat=n):
        total = sum((zh_costs if b == 0 else en_costs)[i] for i, b in enumerate(bits))
        total += SWITCH_COST * sum(
            1 for i in range(1, n) if bits[i] != bits[i - 1]
        )
        key = (total, bits)
        if best is None or key < best:
            best = key
    return best


def test_best_orders_bruteforce_random_costs():
    rng = random.Random(20260925)
    for _ in range(600):
        n = rng.randint(1, 7)
        zh = [rng.randint(0, 6) for _ in range(n)]
        en = [rng.randint(0, 6) for _ in range(n)]
        orders = _best_orders(zh, en)
        bits = tuple(0 if o == ZH_TOP else 1 for o in orders)
        total, expected_bits = _brute_best(zh, en)
        assert bits == expected_bits
        got = sum((zh if b == 0 else en)[i] for i, b in enumerate(bits))
        got += SWITCH_COST * sum(
            1 for i in range(1, n) if bits[i] != bits[i - 1]
        )
        assert got == total


def test_split_subtitles_bruteforce_random_lines():
    rng = random.Random(114514)
    zh_pool = ["你好世界", "今天天气不错", "字幕", "拆轨其实很简单"]
    en_pool = ["Hello world", "See you", "abc def", "XYZ"]
    neutral_pool = ["……", "123", "?!", "…… 100"]
    mixed_pool = ["你好abc", "第3集 ep3", "a好"]
    pools = [zh_pool, en_pool, neutral_pool, mixed_pool]
    for _ in range(200):
        n = rng.randint(1, 6)
        blocks = []
        for i in range(n):
            lines = [rng.choice(rng.choice(pools)) for _ in range(2)]
            blocks.append((i * 2000, i * 2000 + 1500, lines))
        rows = _split(blocks)
        zh_costs, en_costs = [], []
        for _, _, lines in blocks:
            top, bottom = lines
            top_h = sum(1 for c in top if _is_hanzi(c))
            top_l = sum(1 for c in top if _is_latin_letter(c))
            bot_h = sum(1 for c in bottom if _is_hanzi(c))
            bot_l = sum(1 for c in bottom if _is_latin_letter(c))
            zh_costs.append(top_l + bot_h)
            en_costs.append(top_h + bot_l)
        _, expected_bits = _brute_best(zh_costs, en_costs)
        bits = tuple(0 if r.order == ZH_TOP else 1 for r in rows)
        assert bits == expected_bits
        for r, b, (_, _, lines) in zip(rows, bits, blocks):
            top, bottom = lines
            assert r.zh_line == (top if b == 0 else bottom)
            assert r.en_line == (bottom if b == 0 else top)
            assert r.mismatch == (zh_costs if b == 0 else en_costs)[r.new_index - 1]


# ---------- 行数校验 ----------

def test_one_line_block_rejected():
    with pytest.raises(ValueError, match="第 1 块.*恰好两行.*实际 1 行"):
        _split([(1000, 2000, ["只有一行"])])


def test_three_line_block_rejected():
    with pytest.raises(ValueError, match="第 2 块.*恰好两行.*实际 3 行"):
        _split([
            (1000, 2000, ["你好。", "Hello."]),
            (2500, 3500, ["甲", "乙", "丙"]),
        ])


def test_line_count_error_uses_declared_index():
    with pytest.raises(ValueError, match="第 2 块（声明序号 9）"):
        _split(
            [(1000, 2000, ["你好。", "Hello."]), (2500, 3500, ["只有一行"])],
            declared=[5, 9],
        )


# ---------- 导出 ----------

def test_build_split_srt_renumbers_and_keeps_lines():
    rows = _split(
        [
            (1000, 2500, ["你好，世界！", "Hello, world!"]),
            (9000, 10000, ["See you.", "再见！"]),
        ],
        declared=[5, 9],
    )
    zh = build_split_srt(rows, "zh")
    zh_blocks = zh.strip().split("\n\n")
    assert [b.splitlines() for b in zh_blocks] == [
        ["1", "00:00:01,000 --> 00:00:02,500", "你好，世界！"],
        ["2", "00:00:09,000 --> 00:00:10,000", "再见！"],
    ]
    en = build_split_srt(rows, "en")
    en_blocks = en.strip().split("\n\n")
    assert [b.splitlines() for b in en_blocks] == [
        ["1", "00:00:01,000 --> 00:00:02,500", "Hello, world!"],
        ["2", "00:00:09,000 --> 00:00:10,000", "See you."],
    ]


def test_build_split_srt_rejects_unknown_track():
    rows = _split([(1000, 2000, ["你好。", "Hello."])])
    with pytest.raises(ValueError, match="未知轨道"):
        build_split_srt(rows, "fr")


# ---------- 接口 ----------

SRT_BILINGUAL = _srt([
    (1000, 3000, ["你好，世界！", "Hello, world!"]),
    (3500, 5500, ["今天我们来聊聊双语字幕。", "Today we talk about bilingual subtitles."]),
    (6000, 8000, ["Splitting tracks is easy.", "拆轨其实很简单。"]),
    (8500, 10000, ["……", "……"]),
    (10500, 12500, ["再见！", "See you!"]),
])


def test_api_split_preview():
    resp = client.post("/api/split", json={"srt": SRT_BILINGUAL})
    assert resp.status_code == 200
    data = resp.json()
    s = data["summary"]
    assert s["total"] == 5
    assert s["mismatch_chars"] == 0
    assert s["switches"] == 2          # 块2→3、块3→4 各变一次
    assert s["switch_cost"] == 4
    assert s["total_cost"] == 4
    rows = data["rows"]
    assert [r["new_index"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["order"] for r in rows] == [
        "zh_top", "zh_top", "en_top", "zh_top", "zh_top",
    ]
    assert [r["switched"] for r in rows] == [False, False, True, True, False]
    assert rows[2]["zh_line"] == "拆轨其实很简单。"
    assert rows[2]["en_line"] == "Splitting tracks is easy."
    assert rows[0]["start_tc"] == "00:00:01,000"
    assert rows[0]["end_tc"] == "00:00:03,000"


def test_api_split_download_tracks():
    resp = client.post("/api/split.zh.srt", json={"srt": SRT_BILINGUAL})
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    zh = resp.content.decode("utf-8")
    zh_blocks = zh.strip().split("\n\n")
    assert len(zh_blocks) == 5
    assert zh_blocks[0].splitlines() == [
        "1", "00:00:01,000 --> 00:00:03,000", "你好，世界！",
    ]
    assert zh_blocks[2].splitlines() == [
        "3", "00:00:06,000 --> 00:00:08,000", "拆轨其实很简单。",
    ]

    resp = client.post("/api/split.en.srt", json={"srt": SRT_BILINGUAL})
    assert resp.status_code == 200
    en = resp.content.decode("utf-8")
    en_blocks = en.strip().split("\n\n")
    assert len(en_blocks) == 5
    assert en_blocks[0].splitlines() == [
        "1", "00:00:01,000 --> 00:00:03,000", "Hello, world!",
    ]
    assert en_blocks[2].splitlines() == [
        "3", "00:00:06,000 --> 00:00:08,000", "Splitting tracks is easy.",
    ]


def test_api_split_bad_line_count_400_points_block():
    srt = _srt([
        (1000, 2000, ["你好。", "Hello."]),
        (2500, 3500, ["只有一行"]),
    ])
    for path in ("/api/split", "/api/split.zh.srt", "/api/split.en.srt"):
        resp = client.post(path, json={"srt": srt})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "第 2 块" in detail
        assert "恰好两行" in detail


def test_api_split_empty_and_bad_srt():
    resp = client.post("/api/split", json={"srt": ""})
    assert resp.status_code == 400
    resp = client.post("/api/split", json={"srt": "没有时间码"})
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]
