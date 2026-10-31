#!/usr/bin/env python3
"""决定性实验：固定 qp 扫描——追平 ProRes 同档边缘指标需要多大体积代价。

对 2K 母版第 100 帧逐 qp 编码（encode_sized 钉死 qp=min=max）→ 解码回
重建 → 对比同一帧的 ProRes 基准：边缘 PSNR / 8×8 网格残留 / 色度 PSNR
/ 每帧字节。回答："如果不用空间自适应、靠全局降 qp 硬追，体积要多花多少？"
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import av
import numpy as np
from scipy.ndimage import binary_dilation, laplace, sobel

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
)
from diagnose_edge_jaggies import (  # noqa: E402
    MEDIA,
    _plane_array,
    grid_signature,
)

W, H = 1920, 1080
FRAME = 100


def grab_master_planes():
    with av.open(str(MEDIA / "4K_prores4444.mov")) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if (f.width, f.height) != (W, H):
                f = f.reformat(width=W, height=H, format="yuv422p10le")
            elif f.format.name != "yuv422p10le":
                f = f.reformat(format="yuv422p10le")
            if i == FRAME:
                return [_plane_array(p) for p in f.planes[:3]]
    raise RuntimeError("frame not found")


def grab_prores_frame(path: Path):
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if f.format.name != "yuv422p10le":
                f = f.reformat(format="yuv422p10le")
            if i == FRAME:
                return [_plane_array(p) for p in f.planes[:3]]
    raise RuntimeError("frame not found")


def prores_frame_bytes(path: Path) -> int:
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        return sum(p.buffer_size for p in c.demux(s)) // 487


def edge_metrics(ref_y, tst_y, ref, tst):
    f = ref_y.astype(np.float64)
    mag = np.hypot(sobel(f, 0), sobel(f, 1))
    mask = binary_dilation(mag > max(30.0, float(np.percentile(mag, 96.0))), iterations=1)
    res = tst_y.astype(np.float64) - f
    mse_e = float(np.mean(res[mask] ** 2))
    psnr_e = 10 * np.log10(1023**2 / mse_e) if mse_e > 0 else float("nan")
    le = float(np.mean(laplace(f)[mask] ** 2))
    lt = float(np.mean(laplace(tst_y.astype(np.float64))[mask] ** 2))
    g = grid_signature(res, mask)
    out = {
        "psnr_edge": round(psnr_e, 2),
        "grid": round(g, 3),
        "sharp": round(lt / le, 3),
    }
    for k, name in ((1, "u"), (2, "v")):
        d = tst[k].astype(np.float64) - ref[k].astype(np.float64)
        mse = float(np.mean(d * d))
        out[f"{name}_psnr"] = round(10 * np.log10(1023**2 / mse) if mse > 0 else float("nan"), 2)
    return out


def main() -> int:
    ref = grab_master_planes()
    ref_y = ref[0]
    prores_proxy = grab_prores_frame(MEDIA / "2K_prores422Proxy.mov")
    prores_std = grab_prores_frame(MEDIA / "2K_prores422.mov")
    pr_proxy_bytes = prores_frame_bytes(MEDIA / "2K_prores422Proxy.mov")
    pr_std_bytes = prores_frame_bytes(MEDIA / "2K_prores422.mov")

    codec = ToposCodec()
    cfg = topos_binding._CFrameConfig()
    cfg.struct_size = ctypes.sizeof(cfg)
    cfg.abi_version = TOPOS_CODEC_ABI_VERSION
    cfg.visible_width = W
    cfg.visible_height = H
    cfg.qmatrix_id = 1
    cfg.slice_rows = 8

    print(f"ProRes proxy : {edge_metrics(ref_y, prores_proxy[0], ref, prores_proxy)} "
          f"frame_bytes={pr_proxy_bytes}")
    print(f"ProRes std   : {edge_metrics(ref_y, prores_std[0], ref, prores_std)} "
          f"frame_bytes={pr_std_bytes}")
    print(f"{'qp':>4} {'bytes':>8} {'vs_PRproxy':>10} {'psnr_edge':>9} {'grid':>6} "
          f"{'sharp':>6} {'u_psnr':>7} {'v_psnr':>7}")
    for qp in range(86, 63, -2):
        pkt, stats, qp_used = codec.encode_sized(cfg, ref, 1 << 30, qp, qp)
        assert qp_used == qp, (qp_used, qp)
        frame = codec.decode(bytes(pkt))
        planes = [np.frombuffer(frame.planes[k], dtype="<u2").reshape(
            H, W if k == 0 else W // 2) for k in range(3)]
        m = edge_metrics(ref_y, planes[0], ref, planes)
        size = stats.packet_size
        print(f"{qp:>4} {size:>8} {size / pr_proxy_bytes:>9.2f}x {m['psnr_edge']:>9} "
              f"{m['grid']:>6} {m['sharp']:>6} {m['u_psnr']:>7} {m['v_psnr']:>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
