"""剪辑字幕重建：裁切/平移/重叠去重算法单元测试 + 接口测试。

运行：python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers import parse_srt
from app.rebuild import (
    Clip,
    build_rebuild_srt,
    rebuild_subtitles,
    validate_clips,
)

client = TestClient(app)


def _c(si, so, ti):
    return Clip(source_in=si, source_out=so, target_in=ti)


SRT = "\n".join(
    [
        "1",
        "00:00:00,500 --> 00:00:02,000",
        "第一句",
        "",
        "2",
        "00:00:03,000 --> 00:00:06,000",
        "第二句上行",
        "第二句下行",
        "",
        "3",
        "00:00:07,000 --> 00:00:09,000",
        "第三句",
        "",
    ]
)


def _items():
    return parse_srt(SRT)


# ---------- 剪辑清单校验 ----------

def test_validate_clips_ok():
    clips = validate_clips(
        [
            {"source_in": 0, "source_out": 1000, "target_in": 0},
            {"source_in": 5000, "source_out": 6000, "target_in": 200000},
        ]
    )
    assert clips == [_c(0, 1000, 0), _c(5000, 6000, 200000)]


def test_validate_clips_not_list():
    with pytest.raises(ValueError, match="JSON 数组"):
        validate_clips({"source_in": 0})


def test_validate_clips_empty():
    with pytest.raises(ValueError, match="至少提供一段"):
        validate_clips([])


@pytest.mark.parametrize(
    "bad,msg",
    [
        ([{"source_out": 1000, "target_in": 0}], "缺少字段 source_in"),
        ([{"source_in": 0, "source_out": 1000}], "缺少字段 target_in"),
        ([{"source_in": -1, "source_out": 1000, "target_in": 0}], "第 1 项 source_in 不能为负"),
        ([{"source_in": 1.5, "source_out": 1000, "target_in": 0}], "第 1 项 source_in 须为非负整数"),
        ([{"source_in": "1", "source_out": 1000, "target_in": 0}], "第 1 项 source_in 须为非负整数"),
        ([{"source_in": True, "source_out": 1000, "target_in": 0}], "第 1 项 source_in 须为非负整数"),
        ([{"source_in": 1000, "source_out": 1000, "target_in": 0}], "第 1 项 source_out"),
        ([{"source_in": 2000, "source_out": 1000, "target_in": 0}], "第 1 项 source_out"),
        ([{"source_in": 0, "source_out": -5, "target_in": 0}], "第 1 项 source_out 不能为负"),
        ("not-object", "第 2 项须为 JSON 对象"),
    ],
)
def test_validate_clips_rejects(bad, msg):
    clips = bad if isinstance(bad, list) else [
        {"source_in": 0, "source_out": 100, "target_in": 0}, bad
    ]
    with pytest.raises(ValueError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        validate_clips(clips)


def test_validate_clips_points_to_later_item():
    # 出错的是第 2 项时错误信息必须指出项号
    with pytest.raises(ValueError, match="第 2 项 source_out"):
        validate_clips(
            [
                {"source_in": 0, "source_out": 100, "target_in": 0},
                {"source_in": 50, "source_out": 50, "target_in": 0},
            ]
        )


# ---------- 基本裁切与平移 ----------

def test_basic_clip_cuts_and_shifts():
    # 素材 [500,2000) -> 目标 [10000,11500)，块 1 完整落入
    rows = rebuild_subtitles(_items(), [_c(0, 3000, 10000)])
    assert len(rows) == 1
    r = rows[0]
    assert r.start_ms == 10500 and r.end_ms == 12000
    assert r.source_indices == (1,)
    assert r.body_lines == ("第一句",)
    assert r.start_tc == "00:00:10,500" and r.end_tc == "00:00:12,000"


def test_partial_intersection_head_and_tail():
    # 裁掉块 2 的头部 500ms 与尾部 1000ms，保留 [3500,5000) -> [0,1500)
    rows = rebuild_subtitles(_items(), [_c(3500, 5000, 0)])
    assert len(rows) == 1
    r = rows[0]
    assert (r.start_ms, r.end_ms) == (0, 1500)
    assert r.source_indices == (2,)
    assert r.body_lines == ("第二句上行", "第二句下行")  # 块内换行保留


def test_empty_intersection_dropped():
    # 与所有块空交集（左闭右开：2000 是块 1 出点，3000 是块 2 入点）
    assert rebuild_subtitles(_items(), [_c(2000, 3000, 0)]) == []
    # 仅端点相触也算空交集
    assert rebuild_subtitles(_items(), [_c(2000, 2000 + 1, 0)]) == []
    assert rebuild_subtitles(_items(), [_c(2999, 3000, 0)]) == []


def test_endpoint_touch_is_included_at_in_point():
    # 素材入点恰好等于块 2 入点 3000（左闭）：相交
    rows = rebuild_subtitles(_items(), [_c(3000, 4000, 0)])
    assert len(rows) == 1
    assert rows[0].source_indices == (2,)


# ---------- 乱序、多块、序位排列 ----------

def test_unordered_clips_sorted_on_target_timeline():
    # 先给后面的素材、再给前面的素材，输出仍按目标时间排列
    rows = rebuild_subtitles(
        _items(),
        [_c(7000, 9000, 5000), _c(500, 2000, 0)],
    )
    assert [r.source_indices for r in rows] == [(1,), (3,)]
    assert rows[0].start_ms == 0 and rows[1].start_ms == 5000


def test_multiple_blocks_in_one_segment_sorted_by_parse_order():
    # 一段素材同时覆盖块 2（3000-6000）与块 3（7000-9000）；
    # 中间 6000-7000 无字幕，切成两条，且不跨空隙合并
    rows = rebuild_subtitles(_items(), [_c(0, 10000, 0)])
    assert [r.source_indices for r in rows] == [(1,), (2,), (3,)]
    assert [(r.start_ms, r.end_ms) for r in rows] == [
        (500, 2000), (3000, 6000), (7000, 9000),
    ]


def test_overlapping_target_intervals_concatenate_blocks():
    # 块 1 -> 目标 [500,2000)；块 2 -> 目标 [1000,4000)，在 [1000,2000) 重叠
    rows = rebuild_subtitles(
        _items(),
        [_c(0, 3000, 0), _c(3000, 6000, 1000)],
    )
    assert [(r.start_ms, r.end_ms, r.source_indices) for r in rows] == [
        (500, 1000, (1,)),
        (1000, 2000, (1, 2)),
        (2000, 4000, (2,)),
    ]
    # 重叠段按解析序位连接，保留各块正文换行
    assert rows[1].body_lines == ("第一句", "第二句上行", "第二句下行")


# ---------- 同一原块多片段覆盖：只显示一次，最后一份覆盖结束才移除 ----------

def test_duplicate_clip_same_block_shown_once_when_identical():
    # 同一段素材重复使用、目标位置完全相同：覆盖计数为 2，正文只出现一次
    rows = rebuild_subtitles(
        _items(),
        [_c(500, 2000, 500), _c(500, 2000, 500)],
    )
    assert len(rows) == 1
    assert rows[0].source_indices == (1,)
    assert (rows[0].start_ms, rows[0].end_ms) == (500, 2000)

def test_duplicate_clip_union_overlapping_coverage():
    # 同一块经两段剪辑映射到重叠的目标区间：[0,1500) 与 [1000,2500)
    # 并集为 [0,2500)，中间不出现空隙、不重复正文
    rows = rebuild_subtitles(
        _items(),
        [_c(500, 2000, 0), _c(500, 2000, 1000)],
    )
    assert len(rows) == 1
    r = rows[0]
    assert (r.start_ms, r.end_ms) == (0, 2500)
    assert r.source_indices == (1,)
    assert r.body_lines == ("第一句",)


def test_duplicate_clip_disjoint_coverage_gap_inside():
    # 同一原块映射到两个互不相邻的目标区间：[0,1000) 与 [5000,6000)，
    # 中间无该块覆盖，应切成两条（不会跨空隙合并）
    rows = rebuild_subtitles(
        _items(),
        [_c(500, 1500, 0), _c(1000, 2000, 5000)],
    )
    assert [(r.start_ms, r.end_ms, r.source_indices) for r in rows] == [
        (0, 1000, (1,)),
        (5000, 6000, (1,)),
    ]


def test_block_removed_only_after_last_coverage_ends():
    # 块 1 映射到 [0,1500) 与 [2000,3500)；块 3 映射到 [1000,3000)。
    # 第一份块 1 在 1500 结束后块 1 暂时消失；第二份自 2000 起再次有效，
    # [2000,3000) 集合为 {1,3}（按序位 1,3 排列），3000 后仅剩块 1 到 3500。
    rows = rebuild_subtitles(
        _items(),
        [_c(500, 2000, 0), _c(500, 2000, 2000), _c(7000, 9000, 1000)],
    )
    assert [(r.start_ms, r.end_ms, r.source_indices) for r in rows] == [
        (0, 1000, (1,)),
        (1000, 1500, (1, 3)),
        (1500, 2000, (3,)),
        (2000, 3000, (1, 3)),
        (3000, 3500, (1,)),
    ]


# ---------- 相邻同集合合并 ----------

def test_adjacent_same_set_intervals_merge():
    # 块 1 完整一份覆盖 [0,1500)，另一份只从素材 1500 起映射到目标 [1500,2000)：
    # [0,1500) 集合 {1}，[1500,2000) 集合 {1}（第一份结束、第二份开始在同一端点），
    # 相邻且集合相同 -> 合并为一条 [0,2000)
    rows = rebuild_subtitles(
        _items(),
        [_c(500, 2000, 0), _c(1500, 2000, 1500)],
    )
    assert len(rows) == 1
    assert (rows[0].start_ms, rows[0].end_ms) == (0, 2000)


def test_adjacent_different_set_not_merged():
    # [0,1500) 仅块 1；[1500,4000) 仅块 2，相邻但集合不同
    rows = rebuild_subtitles(
        _items(),
        [_c(500, 2000, 0), _c(4500, 6000, 1500)],
    )
    assert [(r.start_ms, r.end_ms, r.source_indices) for r in rows] == [
        (0, 1500, (1,)),
        (1500, 3000, (2,)),
    ]


# ---------- SRT 导出 ----------

def test_build_srt_numbering_and_newlines():
    rows = rebuild_subtitles(
        _items(),
        [_c(0, 3000, 0), _c(3000, 6000, 1000)],
    )
    out = build_rebuild_srt(rows)
    blocks = out.strip().split("\n\n")
    assert blocks[0].splitlines() == [
        "1", "00:00:00,500 --> 00:00:01,000", "第一句",
    ]
    assert blocks[1].splitlines() == [
        "2", "00:00:01,000 --> 00:00:02,000",
        "第一句", "第二句上行", "第二句下行",
    ]
    assert blocks[2].splitlines() == [
        "3", "00:00:02,000 --> 00:00:04,000",
        "第二句上行", "第二句下行",
    ]


def test_build_empty_srt():
    assert build_rebuild_srt([]) == ""


# ---------- 接口 ----------

CLIPS = [
    # 故意乱序：后段素材在前；第 1 段素材重复使用且目标区间与块 3 重叠
    {"source_in": 3000, "source_out": 6000, "target_in": 2000},
    {"source_in": 0, "source_out": 3000, "target_in": 0},
    {"source_in": 0, "source_out": 3000, "target_in": 1500},
]


def test_api_rebuild_preview():
    resp = client.post("/api/rebuild", json={"srt": SRT, "clips": CLIPS})
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["clips"] == 3
    rows = data["rows"]
    # 第一份块 1 在 1500 结束；第二份块 1 与块 2 在 [2000,3500) 重叠
    assert [(r["start_tc"], r["end_tc"], r["source_indices"]) for r in rows] == [
        ("00:00:00,500", "00:00:02,000", [1]),
        ("00:00:02,000", "00:00:03,500", [1, 2]),
        ("00:00:03,500", "00:00:05,000", [2]),
    ]
    assert data["summary"]["total"] == 3
    assert data["summary"]["source_blocks"] == 2
    assert [r["new_index"] for r in rows] == [1, 2, 3]
    # 多行正文用 \n 连接；重叠段按解析序位把两块正文相接
    assert rows[1]["text"] == "第一句\n第二句上行\n第二句下行"


def test_api_rebuild_srt_download():
    resp = client.post("/api/rebuild.srt", json={"srt": SRT, "clips": CLIPS})
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert 'filename="rebuilt.srt"' in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    blocks = text.strip().split("\n\n")
    assert len(blocks) == 3
    assert blocks[0].splitlines()[:2] == ["1", "00:00:00,500 --> 00:00:02,000"]
    assert blocks[-1].splitlines()[:2] == ["3", "00:00:03,500 --> 00:00:05,000"]


def test_api_rebuild_empty_result():
    # 合法但与所有字幕不相交：预览 0 条，导出空 SRT
    resp = client.post(
        "/api/rebuild",
        json={"srt": SRT, "clips": [{"source_in": 2000, "source_out": 3000, "target_in": 0}]},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == []
    assert resp.json()["summary"] == {"total": 0, "clips": 1, "source_blocks": 0}
    dl = client.post(
        "/api/rebuild.srt",
        json={"srt": SRT, "clips": [{"source_in": 2000, "source_out": 3000, "target_in": 0}]},
    )
    assert dl.status_code == 200
    assert dl.content.decode("utf-8") == ""


def test_api_rebuild_bad_clip_400_points_item():
    resp = client.post(
        "/api/rebuild",
        json={
            "srt": SRT,
            "clips": [
                {"source_in": 0, "source_out": 100, "target_in": 0},
                {"source_in": 50, "source_out": 50, "target_in": 0},
            ],
        },
    )
    assert resp.status_code == 400
    assert "第 2 项" in resp.json()["detail"]


def test_api_rebuild_negative_400():
    resp = client.post(
        "/api/rebuild",
        json={
            "srt": SRT,
            "clips": [{"source_in": -1, "source_out": 100, "target_in": 0}],
        },
    )
    assert resp.status_code == 400
    assert "第 1 项" in resp.json()["detail"]


def test_api_rebuild_missing_field_400():
    resp = client.post(
        "/api/rebuild",
        json={"srt": SRT, "clips": [{"source_in": 0, "source_out": 100}]},
    )
    assert resp.status_code == 400
    assert "第 1 项缺少字段 target_in" in resp.json()["detail"]


def test_api_rebuild_float_400():
    resp = client.post(
        "/api/rebuild",
        json={
            "srt": SRT,
            "clips": [{"source_in": 0.5, "source_out": 100, "target_in": 0}],
        },
    )
    assert resp.status_code == 400
    assert "第 1 项 source_in" in resp.json()["detail"]


def test_api_rebuild_clips_not_array_400():
    resp = client.post(
        "/api/rebuild",
        json={"srt": SRT, "clips": {"source_in": 0, "source_out": 100, "target_in": 0}},
    )
    assert resp.status_code == 400
    assert "JSON 数组" in resp.json()["detail"]


def test_api_rebuild_empty_clips_400():
    resp = client.post("/api/rebuild", json={"srt": SRT, "clips": []})
    assert resp.status_code == 400


def test_api_rebuild_empty_srt_400():
    resp = client.post(
        "/api/rebuild",
        json={"srt": "", "clips": [{"source_in": 0, "source_out": 100, "target_in": 0}]},
    )
    assert resp.status_code == 400


def test_api_rebuild_bad_srt_400():
    resp = client.post(
        "/api/rebuild",
        json={
            "srt": "没有时间码的文本",
            "clips": [{"source_in": 0, "source_out": 100, "target_in": 0}],
        },
    )
    assert resp.status_code == 400
    assert "SRT" in resp.json()["detail"]
