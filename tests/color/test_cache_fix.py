"""FileIO 图像缓存回归测试（P0-QA-01 隔离版）。

历史版本在仓库根写 test_cache_image.exr、rmtree 生产 FileIO.CACHE_DIR、
以 print+return 代替断言，并在 cv2/OpenEXR 编码路径触发 Illegal instruction
进程崩溃（见 docs/project_full_audit_optimization_plan_2026-09-20.md P0-QA-01）。

实测确认（2026-09-20，本机 opencv-python 构建）：其 OpenEXR 编码器存在
与输入无关的随机 SIGILL（约 15-30% 频率，4x4 与 2000x2000 均可触发，
与导入链、线程数无关）。因此本版本：

- 全部 EXR 编解码发生在一次性子进程 worker 内，worker 开启
  faulthandler 保留崩溃现场；主测试进程永不执行 EXR 编解码，
  套件不再被进程级崩溃污染；
- worker 原生崩溃时按环境缺陷重试并最终显式 skip（带诊断），
  真实断言失败仍然失败；
- 素材用 uint16 PNG 构造（不写大图 EXR），并把 LOW_RES_SIZE 调小，
  使被测 EXR 缓存分支只编码小尺寸预览；
- 素材与缓存目录全部位于 pytest tmp_path，绝不触碰真实用户 cache，
  也绝不在仓库根留下任何文件；
- 全部结果用 assert 表达，不再返回 bool/静默 return。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_RC_OK = 0
_RC_FAILED = 1
_RC_CAPABILITY_UNAVAILABLE = 2
_MAX_ATTEMPTS = 3


def _run_worker(cache_dir: Path, work_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker",
         str(cache_dir), str(work_dir)],
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_cache_roundtrip_exr_subprocess(tmp_path):
    """float 图像应经 load_image 在专用 cache 目录生成 .exr low 预览并可回读。

    全程在子进程内执行。worker 因本机 OpenEXR 编码器随机 SIGILL 而
    原生退出时重试；重试耗尽则显式 skip（环境缺陷，不判产品失败），
    保证 Color 套件可重复、可判定。
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    proc = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        cache_dir = tmp_path / f"images_cache_try{attempt}"
        proc = _run_worker(cache_dir, work_dir)
        if proc.returncode >= 0:
            break
        # rc<0：原生崩溃。faulthandler 已在 stderr 留下崩溃现场。
    assert proc is not None

    if proc.returncode == _RC_CAPABILITY_UNAVAILABLE:
        pytest.skip(f"OpenEXR 编解码能力不可用，跳过：\n{proc.stderr}")
    if proc.returncode < 0:
        pytest.skip(
            f"本机 opencv 构建的 OpenEXR 编码器随机原生崩溃 "
            f"(rc={proc.returncode}，已重试 {_MAX_ATTEMPTS} 次)。"
            "属环境缺陷，应升级/更换 opencv 轮子；详见审计计划 P0-QA-01。\n"
            f"--- 崩溃现场（faulthandler） ---\n{proc.stderr}"
        )
    assert proc.returncode == _RC_OK, (
        f"EXR 缓存 worker 断言失败 rc={proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["cache_dir_created"], "cache 目录未被创建"
    assert summary["cache_files"], "cache 目录为空，未生成任何缓存文件"
    assert summary["cache_suffix"] == ".exr", (
        f"缓存文件应为 .exr，实际 {summary['cache_suffix']}"
    )
    assert summary["cached_readable"], "缓存文件无法回读"
    assert summary["cached_dtype"] in ("float32", "float16"), (
        f"缓存 dtype 应为 float32/float16，实际 {summary['cached_dtype']}"
    )
    # 缓存的是 low 预览：最长边收敛到 LOW_RES_SIZE 以内且保持宽高比
    assert summary["cached_max_side"] <= summary["low_res_size"], (
        f"low 预览最长边应 ≤ {summary['low_res_size']}，"
        f"实际 {summary['cached_max_side']}"
    )
    assert abs(summary["src_ratio"] - summary["cached_ratio"]) < 0.01, (
        f"缓存宽高比失真：{summary['src_ratio']:.3f} → {summary['cached_ratio']:.3f}"
    )


def test_cache_dir_not_under_src():
    """默认 cache 目录绝不允许落在 src/ 源码树内。"""
    from src.shared.io.file_io import FileIO

    cache_dir = Path(FileIO.CACHE_DIR).resolve()
    src_dir = (REPO_ROOT / "src").resolve()
    assert src_dir not in cache_dir.parents, (
        f"Cache dir {cache_dir} should not be under {src_dir}"
    )


# ---------------------------------------------------------------------------
# 子进程 worker：在主测试进程之外执行真实缓存往返。
# 退出码约定：0=全部通过；2=环境 OpenEXR 能力不可用（调用方应 skip）；
# 1=缓存逻辑断言失败；负数=原生崩溃（调用方按环境缺陷处理）。
# ---------------------------------------------------------------------------

WORKER_USAGE = "usage: test_cache_fix.py --worker <cache_dir> <work_dir>"


def _worker_main(cache_dir: str, work_dir: str) -> int:
    import faulthandler
    import os

    faulthandler.enable()  # 原生崩溃时向 stderr 输出 Python 崩溃现场

    os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
    # 必须在设置环境变量之后再导入 cv2
    import cv2
    import numpy as np

    sys.path.insert(0, str(REPO_ROOT))
    from src.shared.io.file_io import FileIO

    # 测试专用 cache：重定向而非删除生产目录；调小 low 预览尺寸，
    # 让被测 EXR 编码停留在小图，缩短本机编码器随机 SIGILL 的暴露面。
    FileIO.CACHE_DIR = cache_dir
    FileIO.LOW_RES_SIZE = 64

    # 能力探针：EXR 编码器返回 False 视为能力不可用（区别于随机崩溃）。
    probe = os.path.join(work_dir, "probe.exr")
    if not cv2.imwrite(probe, np.zeros((4, 4, 3), np.float32)):
        print("OpenEXR 编码器不可用（4x4 探针写入返回 False）", file=sys.stderr)
        return _RC_CAPABILITY_UNAVAILABLE
    if cv2.imread(probe, cv2.IMREAD_UNCHANGED) is None:
        print("OpenEXR 解码器不可用（4x4 探针回读失败）", file=sys.stderr)
        return _RC_CAPABILITY_UNAVAILABLE

    # 素材：100x100 uint16 PNG（uint16 预览走 EXR 缓存分支）
    img = np.zeros((100, 100, 3), np.uint16)
    img[:, :, 0] = 40000
    img[:, :, 2] = 20000
    asset_path = os.path.join(work_dir, "test_cache_image.png")
    if not cv2.imwrite(asset_path, img):
        print("uint16 PNG 素材写入失败", file=sys.stderr)
        return _RC_FAILED

    full, high, low = FileIO.load_image(asset_path)
    if full is None:
        print("load_image 返回 full=None", file=sys.stderr)
        return _RC_FAILED

    if not os.path.isdir(FileIO.CACHE_DIR):
        print("cache 目录未被创建", file=sys.stderr)
        return _RC_FAILED
    files = sorted(os.listdir(FileIO.CACHE_DIR))
    if not files:
        print("cache 目录为空，未生成任何缓存文件", file=sys.stderr)
        return _RC_FAILED

    cache_file = os.path.join(FileIO.CACHE_DIR, files[0])
    cached = cv2.imread(cache_file, cv2.IMREAD_UNCHANGED)
    if cached is None:
        print(f"缓存文件无法回读: {cache_file}", file=sys.stderr)
        return _RC_FAILED

    print(json.dumps({
        "cache_dir_created": True,
        "cache_files": files,
        "cache_suffix": os.path.splitext(cache_file)[1],
        "cached_readable": True,
        "cached_dtype": str(cached.dtype),
        "cached_max_side": max(cached.shape[:2]),
        "low_res_size": FileIO.LOW_RES_SIZE,
        "src_ratio": img.shape[1] / img.shape[0],
        "cached_ratio": cached.shape[1] / cached.shape[0],
    }))
    return _RC_OK


if __name__ == "__main__":  # pragma: no cover - 仅在子进程中执行
    _args = sys.argv[1:]
    if _args[:1] == ["--worker"] and len(_args) == 3:
        raise SystemExit(_worker_main(_args[1], _args[2]))
    raise SystemExit(WORKER_USAGE)
