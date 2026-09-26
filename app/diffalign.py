"""SRT 版本差异审校：旧版/新版 SRT 按块序的全局最小代价对齐。

允许的对齐步：1:1、1:2（拆分）、2:1（合并）及单侧缺项（增删）：

- 候选先合并正文再复用 textnorm 归一化，以归一化编辑距离计文本分；
- 拆并（1:2 / 2:1）在文本分外另加 0.1；增删（单侧缺项）各计 0.45；
- 总分相同时优先入出点偏差（两侧入点差 + 出点差，毫秒）更小，再优先 1:1
  —— DP 关键字为 (总分, 入出点偏差, 拆并次数) 的字典序，逐分量累加，
  其中拆并次数即“非 1:1 配对”的次数（越少越优先 1:1）。

优先 numpy 向量化实现；缺少 numpy 时回退纯 Python（结果一致，速度较慢）。
"""
from __future__ import annotations

from .alignment import GAP_COST  # 增删（单侧缺项）固定代价 0.45
from .textnorm import normalize_text, normalized_distance

SPLIT_EXTRA = 0.1   # 拆并（1:2 / 2:1）在文本代价外另加的固定分

# 操作：pair=1:1，split=1:2（旧一块拆为新两块），merge=2:1（旧两块并为新一块），
# gap_a=旧侧缺项（删除），gap_b=新侧缺项（新增）
Ops = list[tuple[str, float, int | None, int | None]]

# 回溯方向标记
_PAIR, _SPLIT, _MERGE, _GAP_A, _GAP_B = 1, 2, 3, 4, 5


def _norms_joined(items, size: int) -> list[str]:
    """每个起始位置的候选归一化文本：先合并 size 个连续块的正文再归一化。"""
    raws = [it.raw_text for it in items]
    return [
        normalize_text(" ".join(raws[i:i + size]))
        for i in range(len(items) - size + 1)
    ]


def time_dev_ms(old, new, op: str, i: int | None, j: int | None) -> int | None:
    """单个配对操作的入出点偏差（毫秒）：两侧入点差 + 出点差；缺项返回 None。"""
    if op == "pair":
        return (abs(old[i].start_ms - new[j].start_ms)
                + abs(old[i].end_ms - new[j].end_ms))
    if op == "split":
        return (abs(old[i].start_ms - new[j].start_ms)
                + abs(old[i].end_ms - new[j + 1].end_ms))
    if op == "merge":
        return (abs(old[i].start_ms - new[j].start_ms)
                + abs(old[i + 1].end_ms - new[j].end_ms))
    return None


try:
    import numpy as np

    from .alignment import _distance_matrix

    _INF = float("inf")

    def _lex_better(cP, cT, cK, bP, bT, bK):
        """逐元素比较：候选键 (cP,cT,cK) 字典序严格小于当前最优键。"""
        return (cP < bP) | ((cP == bP) & ((cT < bT) | ((cT == bT) & (cK < bK))))

    def _lex_choose(cP, cT, cK, cKind, bP, bT, bK, bKind):
        """逐元素：候选键严格更小时替换（直接入边按 pair/split/merge/gap_a
        的固定顺序评估，键完全相同的种类间平局保留先评估者）。"""
        better = _lex_better(cP, cT, cK, bP, bT, bK)
        return (
            np.where(better, cP, bP),
            np.where(better, cT, bT),
            np.where(better, cK, bK),
            np.where(better, cKind, bKind),
        )

    def _align_numpy(old, new) -> Ops:
        n, m = len(old), len(new)
        # 文本距离矩阵走 C++ 批量计算；时间偏差逐行现算，避免三张全矩阵常驻内存
        d11 = _distance_matrix(_norms_joined(old, 1), _norms_joined(new, 1))
        d12 = (_distance_matrix(_norms_joined(old, 1), _norms_joined(new, 2))
               if m >= 2 else None)
        d21 = (_distance_matrix(_norms_joined(old, 2), _norms_joined(new, 1))
               if n >= 2 else None)
        os_ = np.array([it.start_ms for it in old], dtype=np.float64)
        oe = np.array([it.end_ms for it in old], dtype=np.float64)
        ns = np.array([it.start_ms for it in new], dtype=np.float64)
        ne = np.array([it.end_ms for it in new], dtype=np.float64)

        gap = GAP_COST
        back = [bytearray(m + 1) for _ in range(n + 1)]
        back[0][1:] = b"\x05" * m

        # 行状态：(m+1, 3) 数组，三列为 (总分, 入出点偏差, 拆并次数)
        prev2 = None
        prev = np.zeros((m + 1, 3))
        prev[:, 0] = np.arange(m + 1) * gap

        for i in range(1, n + 1):
            r = i - 1
            # —— 直接入边候选（向量化）：pair / split / merge / gap_a ——
            bP = np.full(m + 1, _INF)
            bT = np.full(m + 1, _INF)
            bK = np.full(m + 1, _INF)
            bKind = np.zeros(m + 1, dtype=np.uint8)

            # pair：来自 (i-1, j-1)
            row_t11 = np.abs(os_[r] - ns) + np.abs(oe[r] - ne)
            cP = np.full(m + 1, _INF)
            cT = np.zeros(m + 1)
            cK = np.zeros(m + 1)
            cKind = np.zeros(m + 1, dtype=np.uint8)
            cP[1:] = prev[:m, 0] + d11[r]
            cT[1:] = prev[:m, 1] + row_t11
            cK[1:] = prev[:m, 2]
            cKind[1:] = _PAIR
            bP, bT, bK, bKind = _lex_choose(
                cP, cT, cK, cKind, bP, bT, bK, bKind
            )

            if m >= 2:  # split：来自 (i-1, j-2)，覆盖新版块 j-2、j-1
                row_t12 = np.abs(os_[r] - ns[:-1]) + np.abs(oe[r] - ne[1:])
                cP = np.full(m + 1, _INF)
                cT = np.zeros(m + 1)
                cK = np.zeros(m + 1)
                cKind = np.zeros(m + 1, dtype=np.uint8)
                cP[2:] = prev[:m - 1, 0] + d12[r] + SPLIT_EXTRA
                cT[2:] = prev[:m - 1, 1] + row_t12
                cK[2:] = prev[:m - 1, 2] + 1.0
                cKind[2:] = _SPLIT
                bP, bT, bK, bKind = _lex_choose(
                    cP, cT, cK, cKind, bP, bT, bK, bKind
                )

            if i >= 2:  # merge：来自 (i-2, j-1)，覆盖旧版块 i-2、i-1
                row_t21 = np.abs(os_[r - 1] - ns) + np.abs(oe[r] - ne)
                cP = np.full(m + 1, _INF)
                cT = np.zeros(m + 1)
                cK = np.zeros(m + 1)
                cKind = np.zeros(m + 1, dtype=np.uint8)
                cP[1:] = prev2[:m, 0] + d21[r - 1] + SPLIT_EXTRA
                cT[1:] = prev2[:m, 1] + row_t21
                cK[1:] = prev2[:m, 2] + 1.0
                cKind[1:] = _MERGE
                bP, bT, bK, bKind = _lex_choose(
                    cP, cT, cK, cKind, bP, bT, bK, bKind
                )

            # gap_a：来自 (i-1, j)
            bP, bT, bK, bKind = _lex_choose(
                prev[:, 0] + gap,
                prev[:, 1],
                prev[:, 2],
                np.full(m + 1, _GAP_A, dtype=np.uint8),
                bP, bT, bK, bKind,
            )

            # —— gap_b 左链闭合（逐列标量递推，键在自然分值域精确比较，
            #    不做任何仿射变换，避免减法抵消造成的假平局/假破平局）——
            pp, tt, kk = prev[:, 0].tolist(), prev[:, 1].tolist(), prev[:, 2].tolist()
            dP, dT, dK = bP.tolist(), bT.tolist(), bK.tolist()
            dKind = bKind.tolist()
            curP = [0.0] * (m + 1)
            curT = [0.0] * (m + 1)
            curK = [0.0] * (m + 1)
            row = back[i]
            curP[0] = pp[0] + gap
            curT[0] = tt[0]
            curK[0] = kk[0]
            row[0] = _GAP_A
            for j in range(1, m + 1):
                best_p, best_t, best_k = dP[j], dT[j], dK[j]
                kind = dKind[j]
                lp = curP[j - 1] + gap
                if lp < best_p:
                    best_p, best_t, best_k = lp, curT[j - 1], curK[j - 1]
                    kind = _GAP_B
                elif lp == best_p:
                    lt = curT[j - 1]
                    if lt < best_t or (lt == best_t and curK[j - 1] < best_k):
                        best_t, best_k = lt, curK[j - 1]
                        kind = _GAP_B
                curP[j], curT[j], curK[j] = best_p, best_t, best_k
                row[j] = kind

            prev2 = prev
            prev = np.column_stack((curP, curT, curK))

        ops: Ops = []
        i, j = n, m
        while i > 0 or j > 0:
            d = back[i][j]
            if d == _PAIR:
                ops.append(("pair", float(d11[i - 1, j - 1]), i - 1, j - 1))
                i -= 1
                j -= 1
            elif d == _SPLIT:
                ops.append(
                    ("split", float(d12[i - 1, j - 2]) + SPLIT_EXTRA, i - 1, j - 2)
                )
                i -= 1
                j -= 2
            elif d == _MERGE:
                ops.append(
                    ("merge", float(d21[i - 2, j - 1]) + SPLIT_EXTRA, i - 2, j - 1)
                )
                i -= 2
                j -= 1
            elif d == _GAP_A:
                ops.append(("gap_a", GAP_COST, i - 1, None))
                i -= 1
            else:
                ops.append(("gap_b", GAP_COST, None, j - 1))
                j -= 1
        ops.reverse()
        return ops

    _HAVE_NUMPY = True
except ImportError:  # pragma: no cover - 取决于运行环境
    _HAVE_NUMPY = False


def _align_python(old, new) -> Ops:
    n, m = len(old), len(new)
    on1 = _norms_joined(old, 1)
    on2 = _norms_joined(old, 2)
    nn1 = _norms_joined(new, 1)
    nn2 = _norms_joined(new, 2)
    gap = GAP_COST
    back = [bytearray(m + 1) for _ in range(n + 1)]
    for jj in range(1, m + 1):
        back[0][jj] = _GAP_B

    prev2 = None
    prev1 = [(jj * gap, 0, 0) for jj in range(m + 1)]  # (总分, 偏差, 拆并次数)
    for i in range(1, n + 1):
        o1 = old[i - 1]
        cur = [None] * (m + 1)
        cur[0] = (prev1[0][0] + gap, prev1[0][1], prev1[0][2])
        back[i][0] = _GAP_A
        for j in range(1, m + 1):
            n1 = new[j - 1]
            # 评估顺序即键完全相同时的种类优先级：
            # pair > split > merge > gap_a > gap_b（严格更小才替换）
            p = prev1[j - 1]
            best = (
                p[0] + normalized_distance(on1[i - 1], nn1[j - 1]),
                p[1] + abs(o1.start_ms - n1.start_ms)
                + abs(o1.end_ms - n1.end_ms),
                p[2],
            )
            kind = _PAIR
            if j >= 2:  # split：旧一块 → 新两块
                p = prev1[j - 2]
                key = (
                    p[0] + normalized_distance(on1[i - 1], nn2[j - 2])
                    + SPLIT_EXTRA,
                    p[1] + abs(o1.start_ms - new[j - 2].start_ms)
                    + abs(o1.end_ms - new[j - 1].end_ms),
                    p[2] + 1,
                )
                if key < best:
                    best, kind = key, _SPLIT
            if i >= 2:  # merge：旧两块 → 新一块
                p = prev2[j - 1]
                o0 = old[i - 2]
                key = (
                    p[0] + normalized_distance(on2[i - 2], nn1[j - 1])
                    + SPLIT_EXTRA,
                    p[1] + abs(o0.start_ms - n1.start_ms)
                    + abs(o1.end_ms - n1.end_ms),
                    p[2] + 1,
                )
                if key < best:
                    best, kind = key, _MERGE
            p = prev1[j]  # gap_a：旧侧缺项（删除）
            key = (p[0] + gap, p[1], p[2])
            if key < best:
                best, kind = key, _GAP_A
            p = cur[j - 1]  # gap_b：新侧缺项（新增）
            key = (p[0] + gap, p[1], p[2])
            if key < best:
                best, kind = key, _GAP_B
            cur[j] = best
            back[i][j] = kind
        prev2, prev1 = prev1, cur

    ops: Ops = []
    i, j = n, m
    while i > 0 or j > 0:
        d = back[i][j]
        if d == _PAIR:
            ops.append(
                ("pair", normalized_distance(on1[i - 1], nn1[j - 1]), i - 1, j - 1)
            )
            i -= 1
            j -= 1
        elif d == _SPLIT:
            ops.append(
                (
                    "split",
                    normalized_distance(on1[i - 1], nn2[j - 2]) + SPLIT_EXTRA,
                    i - 1,
                    j - 2,
                )
            )
            i -= 1
            j -= 2
        elif d == _MERGE:
            ops.append(
                (
                    "merge",
                    normalized_distance(on2[i - 2], nn1[j - 1]) + SPLIT_EXTRA,
                    i - 2,
                    j - 1,
                )
            )
            i -= 2
            j -= 1
        elif d == _GAP_A:
            ops.append(("gap_a", gap, i - 1, None))
            i -= 1
        else:
            ops.append(("gap_b", gap, None, j - 1))
            j -= 1
    ops.reverse()
    return ops


def diff_align(old, new) -> Ops:
    """对两版 SRT 做全局最小代价对齐，返回按块序展开的操作列表。

    每项为 (op, cost, i, j)：i/j 为该操作消费的首个旧/新块的 0 起始索引
    （缺项侧为 None）；op ∈ pair / split / merge / gap_a / gap_b。
    """
    if not old:
        return [("gap_b", GAP_COST, None, j) for j in range(len(new))]
    if not new:
        return [("gap_a", GAP_COST, i, None) for i in range(len(old))]
    if _HAVE_NUMPY:
        return _align_numpy(old, new)
    return _align_python(old, new)
