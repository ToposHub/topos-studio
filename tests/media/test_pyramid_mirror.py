"""ADR-C030 验收：pyramid transform 双实现逐样本一致。

Python 镜像独立翻译 ADR-C030 §2 的冻结算术（floor/rdiv3 lifting、级联
表、尾组补位、末级钳位+逃逸），与 C scalar 参考（test_pyramid_transform
--mirror 输出）逐值比对。任何一侧改动 ADR 算术而未同步另一侧，此测试
即红——先红后绿的规范漂移防线。
"""
from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.media_core

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN_CANDIDATES = (
    os.path.join(_ROOT, "native", "topos_codec", "build", "release",
                 "test_pyramid_transform"),
    os.path.join(_ROOT, "native", "topos_codec", "build", "debug",
                 "test_pyramid_transform"),
)


def _binary() -> str:
    explicit = os.environ.get("TOPOS_PYRAMID_TEST_BIN")
    if explicit:
        return explicit
    for path in _BIN_CANDIDATES:
        if os.path.isfile(path):
            return path
    pytest.skip("test_pyramid_transform 未构建")


# —— Python 镜像：ADR-C030 §2 公式的独立实现 ——

def _floor_div(value: int, div: int) -> int:
    return value // div  # Python // 即数学下取整


def _rdiv3(value: int) -> int:
    return _floor_div(value + 1, 3)


def _analyze_f2(row: list[int]) -> tuple[list[int], list[int]]:
    n = len(row)
    dn = n // 2
    ln = (n + 1) // 2
    d = []
    for j in range(dn):
        left = row[2 * j]
        right = row[2 * j] if 2 * j + 2 >= n else row[2 * j + 2]
        d.append(row[2 * j + 1] - _floor_div(left + right, 2))
    low = []
    for j in range(ln):
        dm = d[j - 1] if j > 0 else 0
        dp = d[j] if j < dn else 0
        low.append(row[2 * j] + _floor_div(dm + dp + 2, 4))
    return low, d


def _analyze_f3(row: list[int]) -> tuple[list[int], list[int], list[int]]:
    n = len(row)
    ln = (n + 2) // 3
    low, d0, d1 = [], [], []
    for i in range(ln):
        a = row[3 * i]
        b = a if 3 * i + 1 >= n else row[3 * i + 1]
        c = b if 3 * i + 2 >= n else row[3 * i + 2]
        d0.append(a - b)
        d1.append(c - b)
        low.append(b + _rdiv3(d0[-1] + d1[-1]))
    return low, d0, d1


def _level_geometry(w: int, h: int, factor: int) -> dict:
    if factor == 2:
        lw, dw = (w + 1) // 2, w // 2
        lh, dh = (h + 1) // 2, h // 2
        return {
            "lw": lw, "lh": lh, "sdw": dw,
            "bands": [(lw, dh), (dw, lh), (dw, dh)],
        }
    tw, th = (w + 2) // 3, (h + 2) // 3
    return {
        "lw": tw, "lh": th, "sdw": 2 * tw,
        "bands": [(tw, th)] * 8,
    }


def _cascade(ratio: int) -> list[int]:
    return {2: [2], 3: [3], 4: [2, 2], 6: [3, 2]}[ratio]


def _analyze_level(plane: list[list[int]], factor: int) -> tuple:
    h = len(plane)
    w = len(plane[0])
    geo = _level_geometry(w, h, factor)
    lw, sdw, lh = geo["lw"], geo["sdw"], geo["lh"]
    sl = [[0] * lw for _ in range(h)]
    sd = [[0] * sdw for _ in range(h)]
    for y in range(h):
        row = plane[y]
        if factor == 2:
            low, d = _analyze_f2(row)
            sl[y] = low
            sd[y] = d + [0] * (sdw - len(d))
        else:
            low, d0, d1 = _analyze_f3(row)
            sl[y] = low
            sd[y] = d0 + d1
    bands = [[[0] * bw_ for _ in range(bh_)] for bw_, bh_ in geo["bands"]]
    next_ll = [[0] * lw for _ in range(lh)]

    def column(matrix: list[list[int]], c: int) -> list[int]:
        return [matrix[y][c] for y in range(len(matrix))]

    for c in range(lw):
        col = column(sl, c)
        if factor == 2:
            vlow, vd = _analyze_f2(col)
            for y in range(lh):
                next_ll[y][c] = vlow[y]
            for y in range(h // 2):
                bands[0][y][c] = vd[y]
        else:
            vlow, ve0, ve1 = _analyze_f3(col)
            for y in range(lh):
                next_ll[y][c] = vlow[y]
                bands[0][y][c] = ve0[y]
                bands[1][y][c] = ve1[y]
    for c in range(sdw):
        col = column(sd, c)
        if factor == 2:
            vlow, vd = _analyze_f2(col)
            for y in range(lh):
                bands[1][y][c] = vlow[y]
            for y in range(h // 2):
                bands[2][y][c] = vd[y]
        else:
            hp = c // lw + 1
            band_col = c % lw
            b0 = 3 * (hp - 1) + 2
            vlow, ve0, ve1 = _analyze_f3(col)
            for y in range(lh):
                bands[b0][y][band_col] = vlow[y]
                bands[b0 + 1][y][band_col] = ve0[y]
                bands[b0 + 2][y][band_col] = ve1[y]
    return next_ll, bands


def mirror_analyze(src: list[list[int]], ratio: int, bit_depth: int) -> dict:
    levels: list[tuple] = []
    cur = [row[:] for row in src]
    for factor in _cascade(ratio):
        cur, bands = _analyze_level(cur, factor)
        levels.append(bands)
    max_value = (1 << bit_depth) - 1
    base = [[0] * len(cur[0]) for _ in range(len(cur))]
    escape = [[0] * len(cur[0]) for _ in range(len(cur))]
    for y in range(len(cur)):
        for x in range(len(cur[0])):
            raw = cur[y][x]
            clamped = min(max(raw, 0), max_value)
            base[y][x] = clamped
            escape[y][x] = raw - clamped
    return {"base": base, "escape": escape, "levels": levels}


def _parse_mirror(text: str) -> list[dict]:
    cases: list[dict] = []
    case = None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "CASE":
            case = {
                "ratio": int(parts[1]), "w": int(parts[2]), "h": int(parts[3]),
                "bit_depth": int(parts[4]), "base": {}, "escape": {}, "bands": {},
            }
            cases.append(case)
        elif parts[0] == "BASE":
            case["base_dims"] = (int(parts[1]), int(parts[2]))
        elif parts[0] == "B":
            case["base"][(int(parts[1]), int(parts[2]))] = int(parts[3])
        elif parts[0] == "E":
            case["escape"][(int(parts[1]), int(parts[2]))] = int(parts[3])
        elif parts[0] == "C":
            key = (int(parts[1]), int(parts[2]))
            case["bands"].setdefault(key, {})[(int(parts[3]), int(parts[4]))] = int(parts[5])
        elif parts[0] in ("L", "END"):
            continue
    return cases


def _mirror_source(w: int, h: int, bit_depth: int) -> list[list[int]]:
    max_value = (1 << bit_depth) - 1
    return [
        [(x * 7 + y * 13 + (x * x + y) % 5) % (max_value + 1) for x in range(w)]
        for y in range(h)
    ]


def test_pyramid_mirror_matches_c_scalar() -> None:
    completed = subprocess.run([_binary(), "--mirror"], check=True,
                               capture_output=True, text=True)
    cases = _parse_mirror(completed.stdout)
    assert cases, "mirror 输出为空"
    for case in cases:
        src = _mirror_source(case["w"], case["h"], case["bit_depth"])
        expected = mirror_analyze(src, case["ratio"], case["bit_depth"])
        bh = len(expected["base"])
        bw = len(expected["base"][0])
        assert (bw, bh) == case["base_dims"], (
            f"ratio={case['ratio']} base 几何不一致：C={case['base_dims']} "
            f"mirror=({bw},{bh})"
        )
        mismatches = 0
        for y in range(bh):
            for x in range(bw):
                if case["base"][(x, y)] != expected["base"][y][x]:
                    mismatches += 1
                if case["escape"][(x, y)] != expected["escape"][y][x]:
                    mismatches += 1
        assert mismatches == 0, (
            f"ratio={case['ratio']} base/escape 不一致 {mismatches} 处"
        )
        for k, bands in enumerate(expected["levels"]):
            for b, band in enumerate(bands):
                got = case["bands"][(k, b)]
                for y in range(len(band)):
                    for x in range(len(band[0])):
                        if got[(x, y)] != band[y][x]:
                            mismatches += 1
                assert mismatches == 0, (
                    f"ratio={case['ratio']} level={k} band={b} 系数不一致"
                )
