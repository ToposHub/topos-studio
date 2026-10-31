"""第六轮审计 Q01 回归：decoder 生命周期关闭/切源压力门禁。

审计背景：tests/edit 全量单进程两次在 ~64% 卡死于
``FFmpegSource.__del__ → close → container.close``（分批无失败）——
存在生命周期/测试顺序风险。pytest.ini 已加 ``faulthandler_timeout``
提供挂起时的全线程栈输出；本文件补"关闭/切源压力测试稳定"的显式
回归：真实素材反复 open→decode→close，断言关闭确定性（有界耗时、
GOP 线程退出、可重开）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = [pytest.mark.edit_core, pytest.mark.real_media]

_VIDEOS = Path("tests/test_videos")
_CLOSE_BUDGET_S = 10.0


def _real_video() -> Path:
    for name in ("2K_h264.mp4", "2K_H265.mov"):
        candidate = _VIDEOS / name
        if candidate.exists():
            return candidate
    pytest.skip("tests/test_videos 真实素材不可用")


def _open(path: Path):
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    return FFmpegMediaSource(str(path))


def test_close_is_bounded_and_reopenable():
    """反复 open→decode→close：关闭有界、GOP 线程退出、可立即重开。"""
    video = _real_video()
    for round_index in range(4):
        source = _open(video)
        try:
            # 读两帧驱动 GOP 索引线程启动
            for frame in (0, 1):
                try:
                    source.read_frame(frame)
                except Exception:
                    pass  # 个别编码格式读帧失败不影响生命周期断言
        finally:
            started = time.monotonic()
            source.close()
            elapsed = time.monotonic() - started

        assert elapsed <= _CLOSE_BUDGET_S, (
            f"round {round_index}: close 耗时 {elapsed:.1f}s "
            f"超预算 {_CLOSE_BUDGET_S}s（container.close 与后台线程竞争）"
        )
        gop_thread = getattr(source, "_gop_index_thread", None)
        assert gop_thread is None or not gop_thread.is_alive(), (
            f"round {round_index}: close 后 GOP 索引线程仍在运行"
        )
        assert source._container is None


def test_source_switch_cycle_does_not_leak_open_containers():
    """模拟 Source 页切源：旧源 close 与新源 open 交替，容器数不累积。"""
    import gc

    video = _real_video()
    closed: list = []
    for round_index in range(6):
        source = _open(video)
        try:
            source.read_frame(0)
        except Exception:
            pass
        source.close()
        closed.append(source)
        # 半数轮次强制 GC——审计卡死点在 __del__，确保析构路径也确定性完成
        if round_index % 2 == 1:
            gc.collect()
    assert len(closed) == 6

    # 所有已关闭源不再持有容器引用
    for i, source in enumerate(closed):
        assert source._container is None, f"source[{i}] 容器未释放"


def test_del_path_closes_without_worker_contention():
    """__del__ 路径（不显式 close）：析构不挂起（有界时间）。"""
    import gc

    video = _real_video()
    source = _open(video)
    try:
        source.read_frame(0)
    except Exception:
        pass
    # 不调用 close——交给引用清零后的 __del__。R22（第七轮审计）：
    # 计时必须在 del 之前开始（旧实现 del 后才计时，__del__ 立即执行
    # 时挂起已经发生在计时窗口外）。
    started = time.monotonic()
    del source
    gc.collect()
    elapsed = time.monotonic() - started
    assert elapsed <= _CLOSE_BUDGET_S, (
        f"gc/__del__ 清理耗时 {elapsed:.1f}s 超预算 "
        f"（审计单进程卡死点：__del__→container.close）"
    )


_CHILD_SCENARIO = """
import faulthandler, gc, sys, time

faulthandler.dump_traceback_later({budget}, exit=True)
sys.path.insert(0, {repo!r})

from src.shared.media.ffmpeg_source import FFmpegMediaSource

# close 路径（GOP 索引线程活跃时竞争关闭）
src = FFmpegMediaSource({video!r})
try:
    src.read_frame(0)
except Exception:
    pass
t0 = time.monotonic()
src.close()
close_elapsed = time.monotonic() - t0
assert close_elapsed <= {budget}, f"close hung: {{close_elapsed:.1f}}s"

# __del__ 路径（不显式 close）
s2 = FFmpegMediaSource({video!r})
try:
    s2.read_frame(0)
except Exception:
    pass
t1 = time.monotonic()
del s2
gc.collect()
del_elapsed = time.monotonic() - t1
assert del_elapsed <= {budget}, f"__del__ hung: {{del_elapsed:.1f}}s"
print("LIFECYCLE_OK")
"""


def test_lifecycle_gate_in_subprocess_with_hard_timeout():
    """R22（第七轮审计）：子进程硬超时门禁。

    旧门禁的耗时断言在 close 返回后才执行——永久挂起时断言永不运行，
    faulthandler_timeout 只 dump 栈不会终止。这里把生命周期场景放进独立
    子进程：子进程 ``faulthandler.dump_traceback_later(exit=True)`` 自带
    硬超时（阻塞时 dump 全线程栈并以非零码退出），父进程再兜一层
    ``subprocess.run(timeout=...)``——故意阻塞 close 也保证在预算内
    失败退出且带栈可诊断。
    """
    import subprocess

    video = _real_video()
    repo = str(Path.cwd())
    code = _CHILD_SCENARIO.format(
        budget=_CLOSE_BUDGET_S, repo=repo, video=str(video))
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            timeout=_CLOSE_BUDGET_S * 2 + 30,
            cwd=repo,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"子进程生命周期门禁超时（>{_CLOSE_BUDGET_S * 2 + 30:.0f}s）——"
            "close/__del__ 存在无界等待")
    assert proc.returncode == 0, (
        f"子进程生命周期失败（rc={proc.returncode}）：\n"
        f"{proc.stdout}\n{proc.stderr[-4000:]}"
    )
    assert "LIFECYCLE_OK" in proc.stdout
