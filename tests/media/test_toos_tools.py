"""toos_tools（.toos ↔ PNG 适配器）数值正确性测试。

回归背景（阶段 12 审计二轮，ADR-I009）：刻度契约曾被违反——
- codec 契约（topos_codec.h 输入/输出约定）：彩色平面 = 声明位深满刻度
  （0..2^bd-1），alpha 平面恒为 16-bit 满刻度容器（0..65535）；
- 旧 encode 把 alpha 当彩色缩放（8-bit 不透明 alpha 写成 1020/65535 ≈
  1.6% 不透明度——文件语义错误），且 16-bit PNG 的 `p << 8` 在 uint16 上
  回绕只保留低 8 位；
- 旧 decode 对 alpha 容器做 <<(16-bd) 同样回绕。

本文件用精确数值断言钉死：8/16-bit、RGB/RGBA 的往返与 A16 语义。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

cv2 = pytest.importorskip("cv2", reason="toos_tools PNG 互转依赖 cv2")

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts/toos_tools.py"
CLI = REPO / "native/topos_codec/build/debug/toos"

pytestmark = pytest.mark.skipif(
    not CLI.exists(), reason="toos CLI 未构建（native/topos_codec/build/debug）")


def _run(*args: str) -> None:
    proc = subprocess.run([sys.executable, str(TOOLS), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"{args}\n{proc.stdout}\n{proc.stderr}"


def _write_png(path: Path, r: int, g: int, b: int, a: int | None,
               h: int = 8, w: int = 8, dtype=np.uint16) -> Path:
    chan = [np.full((h, w), v, dtype) for v in (b, g, r)]  # cv2 BGR
    if a is not None:
        chan.append(np.full((h, w), a, dtype))
    cv2.imwrite(str(path), cv2.merge(chan))
    return path


def _read_png(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert img is not None
    return img


HALF_LSB = {10: 32, 12: 8}  # 16-bit 往返 10/12-bit 的最大量化误差（四舍五入）


class TestRoundTrip:
    def test_16bit_rgb(self, tmp_path: Path) -> None:
        src = _write_png(tmp_path / "rgb16.png", 40000, 20000, 1000, None)
        toos = tmp_path / "rgb16.toos"
        back = tmp_path / "rgb16_back.png"
        _run("encode", str(src), "-o", str(toos), "--bit-depth", "10", "--overwrite")
        _run("decode", str(toos), "-o", str(back))
        img = _read_png(back)
        assert img.dtype == np.uint16 and img.shape[-1] == 3
        for got, want, ch in ((img[..., 2], 40000, "R"),
                              (img[..., 1], 20000, "G"),
                              (img[..., 0], 1000, "B")):
            assert int(got[0, 0]) == want or abs(int(got[0, 0]) - want) <= HALF_LSB[10], (
                f"{ch}: {got[0, 0]} != {want}（16-bit 彩色不得只保留低 8 位）")

    def test_16bit_rgba_alpha_exact(self, tmp_path: Path) -> None:
        # alpha 走 A16 无损直通：往返必须逐位相等（旧实现回绕成 16384）
        src = _write_png(tmp_path / "rgba16.png", 30000, 30000, 30000, 40000)
        toos = tmp_path / "rgba16.toos"
        back = tmp_path / "rgba16_back.png"
        _run("encode", str(src), "-o", str(toos), "--bit-depth", "10", "--overwrite")
        _run("decode", str(toos), "-o", str(back))
        img = _read_png(back)
        assert img.shape[-1] == 4
        assert int(img[0, 0, 3]) == 40000, (
            f"alpha 应 A16 直通 40000，得到 {img[0, 0, 3]}")

    def test_8bit_rgba_exact(self, tmp_path: Path) -> None:
        # 8-bit：彩色 v→(v<<8 精确 16-bit)→10-bit→回 PNG 恒为 v<<8；
        # alpha v→v<<8 直通。全部应精确相等（旧实现靠两个错误抵消碰巧相等，
        # 语义上 alpha 是错的——见 test_alpha_container_semantics）
        src = _write_png(tmp_path / "rgba8.png", 200, 100, 50, 255, dtype=np.uint8)
        toos = tmp_path / "rgba8.toos"
        back = tmp_path / "rgba8_back.png"
        _run("encode", str(src), "-o", str(toos), "--bit-depth", "10", "--overwrite")
        _run("decode", str(toos), "-o", str(back))
        img = _read_png(back)
        assert img.dtype == np.uint16 and img.shape[-1] == 4
        assert int(img[0, 0, 2]) == 200 << 8
        assert int(img[0, 0, 1]) == 100 << 8
        assert int(img[0, 0, 0]) == 50 << 8
        assert int(img[0, 0, 3]) == 255 << 8

    def test_alpha_container_semantics(self, tmp_path: Path) -> None:
        """编码产物的 alpha 必须符合 codec A16 契约（0..65535 满刻度）。

        旧实现把 8-bit 不透明 alpha 写成 1020（≈1.6% 不透明度）——文件在
        任何按契约消费的端（app/C CLI/OIIO）都显示为近乎全透明。
        """
        src = _write_png(tmp_path / "a8.png", 128, 128, 128, 255, dtype=np.uint8)
        toos = tmp_path / "a8.toos"
        _run("encode", str(src), "-o", str(toos), "--bit-depth", "10", "--overwrite")
        raw = tmp_path / "a8.raw"
        proc = subprocess.run([str(CLI), "decode", str(toos), "-o", str(raw)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        side = Path(str(raw) + ".json").read_text()
        assert '"channels": "G,B,R,A"' in side
        # sidecar 必须携带 alpha 位深（A16 语义诊断；mode1 → 16）
        assert '"alpha_mode": 1' in side
        assert '"alpha_bit_depth": 16' in side
        buf = np.fromfile(raw, dtype="<u2")
        alpha = buf[3 * 64:]  # 8x8 全分辨率平面
        assert int(alpha[0]) == 255 << 8, (
            f"不透明 alpha 的 A16 容器值应为 65280，得到 {alpha[0]}")

    def test_yuv_rejected_with_reason(self, tmp_path: Path) -> None:
        # 适配器契约：YUV（Preview）转 PNG 必须显式拒绝（不静默猜色彩矩阵）
        raw = tmp_path / "yuv.raw"
        h = w = 16
        cw = (w + 1) // 2
        planes = [np.full((h, w), 300, "<u2"), np.full((h, cw), 300, "<u2"),
                  np.full((h, cw), 300, "<u2")]
        raw.write_bytes(np.concatenate([p.reshape(-1) for p in planes]).tobytes())
        toos = tmp_path / "yuv.toos"
        proc = subprocess.run(
            [str(CLI), "encode", str(raw), "-o", str(toos), "--width", str(w),
             "--height", str(h), "--pixel-format", "0", "--bit-depth", "10",
             "--overwrite"], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        proc = subprocess.run(
            [sys.executable, str(TOOLS), "decode", str(toos),
             "-o", str(tmp_path / "yuv.png")], capture_output=True, text=True)
        assert proc.returncode != 0
        assert "YUV" in (proc.stdout + proc.stderr)


class TestCliDefaultEncoding:
    """CLI 编码默认策略（ADR-I010）：V2+VLC 熵编码 + qp58 视觉无损档。

    回归背景：旧默认 = V1 Rice + qp28（GBR 4:4:4 1080p 实拍素材
    ≈12.3 MB/帧，对 16-bit 容器压缩率仅 ~2%）。新默认启用 spec v2
    canonical VLC（含 AQ/RDO 编码器内部策略，解码侧无新要求），
    4:2:2 10-bit 同素材 ≈3.1 MB/帧（PSNR≥65dB 典型；qp52 档对
    8-bit 源内容逐位无损）。`--entropy v1` 退回旧 Rice 位流。
    """

    H = W = 64
    CW = 32  # 4:2:2 半宽色度

    @staticmethod
    def _source_planes() -> list[np.ndarray]:
        """8-bit 结构化内容（斜坡 + 平色度），<<2 上采为 10-bit 容器值。"""
        rows = np.arange(64, dtype=np.uint16)
        cols = np.arange(64, dtype=np.uint16)
        y8 = ((cols[None, :] * 255 // 64 + rows[:, None] * 128 // 64) % 256)
        u8 = np.tile((cols[:32] * 255 // 32) % 256, (64, 1))
        v8 = np.full((64, 32), 128, np.uint16)
        return [y8, u8, v8]

    @classmethod
    def _write_422_raw(cls, path: Path) -> list[np.ndarray]:
        src8 = cls._source_planes()
        planes = [s << 2 for s in src8]
        np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(path)
        return src8

    def _encode(self, tmp_path: Path, name: str, *extra: str) -> Path:
        raw = tmp_path / "in.raw"
        self._write_422_raw(raw)
        out = tmp_path / name
        proc = subprocess.run(
            [str(CLI), "encode", str(raw), "-o", str(out),
             "--width", str(self.W), "--height", str(self.H),
             "--pixel-format", "0", "--bit-depth", "10", "--alpha-mode", "0",
             "--image-profile", "0", "--overwrite", *extra],
            capture_output=True, text=True)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        return out

    def _decode_planes(self, toos: Path) -> list[np.ndarray]:
        out = toos.with_suffix(".decoded.raw")
        proc = subprocess.run([str(CLI), "decode", str(toos), "-o", str(out)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        buf = np.fromfile(out, dtype="<u2")
        per, cper = self.W * self.H, self.CW * self.H
        assert buf.size == per + 2 * cper
        return [buf[:per].reshape(self.H, self.W),
                buf[per:per + cper].reshape(self.H, self.CW),
                buf[per + cper:].reshape(self.H, self.CW)]

    def test_default_is_visually_lossless_and_8bit_exact(self, tmp_path: Path) -> None:
        src8 = self._write_422_raw(tmp_path / "in.raw")
        got = self._decode_planes(self._encode(tmp_path, "d.toos"))
        for p, s in zip(got, src8):
            # 8-bit 舍回与源逐位一致：默认档误差只允许落在容器幻影位
            assert np.array_equal((p + 2) >> 2, s)
            # rans2 默认（ADR-C038）下 maxerr=2（10-bit 域）= 0.5 个 8-bit LSB，
            # 视觉无损语义不变（VLC 时代为 ≤1）
            assert int(np.abs(p.astype(int) - (s.astype(int) << 2)).max()) <= 2

    def test_qp52_bit_exact_for_8bit_source(self, tmp_path: Path) -> None:
        src8 = self._write_422_raw(tmp_path / "in.raw")
        got = self._decode_planes(self._encode(tmp_path, "q52.toos", "--qp", "52"))
        for p, s in zip(got, src8):
            assert np.array_equal(p, s << 2)  # 10-bit 域逐位一致

    def test_entropy_v1_escape_roundtrip_exact(self, tmp_path: Path) -> None:
        src8 = self._write_422_raw(tmp_path / "in.raw")
        out = self._encode(tmp_path, "v1.toos", "--qp", "28", "--entropy", "v1")
        got = self._decode_planes(out)
        for p, s in zip(got, src8):
            assert np.array_equal(p, s << 2)

    def test_default_smaller_than_v1_at_same_qp(self, tmp_path: Path) -> None:
        # 同 qp 下 VLC 熵编码必须严格小于 V1 Rice（本内容实测 ~15%+）
        d = self._encode(tmp_path, "d.toos", "--qp", "28")
        v1 = self._encode(tmp_path, "v1.toos", "--qp", "28", "--entropy", "v1")
        assert d.stat().st_size < v1.stat().st_size

    def test_default_qp_is_58(self, tmp_path: Path) -> None:
        # CLI 完成行显式报告 qp 与熵模式（诊断面）
        raw = tmp_path / "in.raw"
        self._write_422_raw(raw)
        proc = subprocess.run(
            [str(CLI), "encode", str(raw), "-o", str(tmp_path / "o.toos"),
             "--width", "64", "--height", "64", "--pixel-format", "0",
             "--bit-depth", "10", "--overwrite"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "qp 58" in proc.stdout
        assert "entropy rans2" in proc.stdout  # ADR-C038（2026-09-12）默认 rans2


class TestCliTiers:
    """编码档位（ADR-I011）：--tier 预设 ↔ capability_manifest.tiers 一致。

    tier 是质量/体积预设（命名空间与 image profile Preview/HQ/XQ 分离）；
    manifest 为文档真相源，本类交叉校验 CLI 执行面。
    """

    H = W = 64

    @staticmethod
    def _tiers() -> dict:
        manifest = json.loads((REPO / "docs/image/capability_manifest.json").read_text())
        return manifest["tiers"]

    def _encode_tier(self, tmp_path: Path, tier: str, *extra: str) -> tuple[Path, str]:
        """按档位构造对应形态的合成平面并编码，返回 (输出, CLI stdout)。"""
        t = self._tiers()[tier]
        w, h, cw = self.W, self.H, self.W // 2
        rows = np.arange(w, dtype=np.uint16)
        cols = np.arange(h, dtype=np.uint16)
        if t["pixel_format"] == 0:  # 4:2:2 YUV（8-bit 源 <<2 上采）
            y8 = (cols[:, None] + rows[None, :] * 3) % 256
            u8 = np.tile(rows[:cw] % 256, (h, 1))
            v8 = np.full((h, cw), 128, np.uint16)
            planes = [y8 << 2, u8 << 2, v8 << 2]
        elif t["pixel_format"] == 3:  # TRAW CFA：4 相位平面 R/Gr/Gb/B
            pw, ph = w // 2, h // 2
            prows = np.arange(pw, dtype=np.uint16)
            pcols = np.arange(ph, dtype=np.uint16)
            planes = []
            for i in range(4):
                if t["bit_depth"] == 16:
                    # 批 4：16-bit 档直接生成全域值（计划测试域规则：禁止
                    # <<8 上采推广 12-bit 模式）
                    grad = (pcols[:, None].astype(np.int64) * 4099
                            + prows[None, :].astype(np.int64) * 1573
                            + i * 2654435761) % 65536
                    planes.append(grad.astype(np.uint16))
                else:
                    grad = (pcols[:, None] * 13 + prows[None, :] * 5 + i * 97) % 3800
                    planes.append((np.uint16(150) + grad).astype(np.uint16))
        else:  # GBR 4:4:4（8-bit 源按档位位深上采：10-bit <<2 / 12-bit <<4）
            shift = t["bit_depth"] - 8
            g8 = (cols[:, None] * 5 + rows[None, :]) % 256
            b8 = np.tile(((cols * 2) % 256)[:, None], (1, w))
            r8 = np.tile((rows * 7) % 256, (h, 1))
            planes = [g8 << shift, b8 << shift, r8 << shift]
        raw = tmp_path / f"in_{tier}.raw"
        np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(raw)
        out = tmp_path / f"{tier}.toos"
        proc = subprocess.run(
            [str(CLI), "encode", str(raw), "-o", str(out), "--tier", tier,
             "--width", str(w), "--height", str(h), "--overwrite", *extra],
            capture_output=True, text=True)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        return out, proc.stdout

    def test_manifest_tiers_encode_with_expected_params(self, tmp_path: Path) -> None:
        for tier, spec in self._tiers().items():
            if tier.startswith("$"):
                continue
            out, stdout = self._encode_tier(tmp_path, tier)
            assert f"qp {spec['qp']}," in stdout, tier
            assert f"qm {spec['qmatrix']}," in stdout, tier
            assert f"tier {tier}" in stdout, tier
            # 解码侧像素格式/位深与档位声明一致
            dec = tmp_path / f"{tier}.raw"
            proc = subprocess.run([str(CLI), "decode", str(out), "-o", str(dec)],
                                  capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
            side = json.loads(Path(str(dec) + ".json").read_text())
            assert side["pixel_format"] == spec["pixel_format"], tier
            assert side["bit_depth"] == spec["bit_depth"], tier

    def test_raw16_lossless_domain_roundtrip(self, tmp_path: Path) -> None:
        # 批 4 验收（计划 §批 4）：16-bit linear CFA 全域值往返像素精确
        # （编码端预失真抵消 W 表项舍入偏差；qp26 ∈ 无损域 Q≤19，实测域）
        w, h = self.W, self.H
        pw, ph = w // 2, h // 2
        rng = np.random.default_rng(20260913)
        planes = []
        for i in range(4):
            grad = (np.arange(pw, dtype=np.int64)[None, :] * 4099
                    + np.arange(ph, dtype=np.int64)[:, None] * 1573
                    + i * 2654435761) % 65536
            noise = rng.integers(0, 256, size=(ph, pw)).astype(np.uint16)
            planes.append((grad.astype(np.uint16) + noise).astype(np.uint16))
        planes[0][0, 0] = 65535   # uint16 满刻度探针
        planes[0][0, 1] = 0       # 零刻度探针
        raw = tmp_path / "in_raw16.raw"
        np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(raw)
        out = tmp_path / "raw16.toos"
        proc = subprocess.run(
            [str(CLI), "encode", str(raw), "-o", str(out),
             "--width", str(w), "--height", str(h), "--pixel-format", "3",
             "--bit-depth", "16", "--qmatrix", "0", "--qp", "26", "--overwrite"],
            capture_output=True, text=True)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        dec = tmp_path / "raw16.dec.raw"
        proc = subprocess.run([str(CLI), "decode", str(out), "-o", str(dec)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        got = np.fromfile(dec, dtype="<u2").reshape(4, ph, pw)
        for a, b in zip(planes, got):
            assert int(np.abs(a.astype(int) - b.astype(int)).max()) == 0

    def test_422_family_size_ordered_low_to_ultra(self, tmp_path: Path) -> None:
        sizes = []
        for tier in ("422-low", "422-medium", "422-high", "422-ultra"):
            out, _ = self._encode_tier(tmp_path, tier)
            sizes.append(out.stat().st_size)
        assert sizes == sorted(sizes), sizes

    def test_ultra_tiers_quality_above_high(self, tmp_path: Path) -> None:
        # ADR-I011 修订三：ultra = 家族内最高画质档（非无损锚，产品语义
        # 『很好画质 + 相对小体积』）——同输入下 PSNR(ultra) > PSNR(high)
        for hi, ul, bd in (("422-high", "422-ultra", 10), ("444-high", "444-ultra", 10)):
            out_hi, _ = self._encode_tier(tmp_path, hi)
            out_ul, _ = self._encode_tier(tmp_path, ul)
            # 同家族合成输入逐字节相同（内容只由 pixel_format 决定）
            ref = np.fromfile(tmp_path / f"in_{hi}.raw", dtype="<u2")
            assert np.array_equal(np.fromfile(tmp_path / f"in_{ul}.raw", dtype="<u2"), ref)
            ref = ref.astype(np.float64)

            def psnr(path: Path) -> float:
                dec = tmp_path / f"{path.stem}_psnr.raw"
                subprocess.run([str(CLI), "decode", str(path), "-o", str(dec)],
                               check=True, capture_output=True)
                got = np.fromfile(dec, dtype="<u2").astype(np.float64)
                mse = float(np.mean((got - ref) ** 2))
                return 999.0 if mse == 0.0 else 10.0 * math.log10(
                    ((1 << bd) - 1.0) ** 2 / mse)

            p_hi, p_ul = psnr(out_hi), psnr(out_ul)
            # rans2 默认（ADR-C038）后 ultra/high 在合成输入上均可数学无损
            #（PSNR=999 哨兵值）——契约退化为不差于 high
            assert p_ul >= p_hi, (hi, ul, p_hi, p_ul)

    def test_default_encode_equals_422_ultra(self, tmp_path: Path) -> None:
        # ADR-I011 修订三：无 --tier 默认（qm0 qp58 + V2 VLC）≡ 422-ultra
        out_ul, _ = self._encode_tier(tmp_path, "422-ultra")
        plain = tmp_path / "plain.toos"
        proc = subprocess.run(
            [str(CLI), "encode", str(tmp_path / "in_422-ultra.raw"), "-o", str(plain),
             "--width", str(self.W), "--height", str(self.H), "--overwrite"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert plain.read_bytes() == out_ul.read_bytes()

    def test_explicit_qp_overrides_tier(self, tmp_path: Path) -> None:
        _, stdout = self._encode_tier(tmp_path, "422-low", "--qp", "40")
        assert "qp 40," in stdout
        assert "tier 422-low" in stdout

    def test_unknown_tier_rejected_with_candidates(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [str(CLI), "encode", "x.raw", "-o", str(tmp_path / "o.toos"),
             "--width", "64", "--height", "64", "--tier", "nope"],
            capture_output=True, text=True)
        assert proc.returncode == 2
        assert "422-low" in proc.stderr and "444-ultra" in proc.stderr
