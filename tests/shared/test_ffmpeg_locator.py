"""ffmpeg_locator 单元测试：打包目录优先、PATH 兜底的二进制定位契约。"""

from __future__ import annotations

import sys

import pytest

from src.shared import ffmpeg_locator


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """每个用例使用独立缓存，且不读真实环境变量。"""
    monkeypatch.delenv(ffmpeg_locator._ENV_HOME, raising=False)
    ffmpeg_locator.clear_cache()
    yield
    ffmpeg_locator.clear_cache()


def _write_executable(directory, exe: str) -> None:
    """创建带执行位的候选二进制（AUD-33703 后定位器校验 X_OK）。"""
    path = directory / exe
    path.write_bytes(b"")
    path.chmod(0o755)


def test_env_home_takes_priority(tmp_path, monkeypatch) -> None:
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bundled = tmp_path / "custom"
    bundled.mkdir()
    _write_executable(bundled, exe)
    monkeypatch.setenv(ffmpeg_locator._ENV_HOME, str(bundled))

    assert ffmpeg_locator.find_ffmpeg() == str(bundled / exe)


def test_bundled_bin_dir_found_without_path(tmp_path, monkeypatch) -> None:
    """打包目录（仓库根/bin）优先于 PATH；PATH 上找不到也能发现。"""
    exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    bundled_bin = tmp_path / "bin"
    bundled_bin.mkdir()
    _write_executable(bundled_bin, exe)

    # 仓库根候选 = locator 模块上两级；把候选目录注入环境变量以隔离测试
    monkeypatch.setenv(ffmpeg_locator._ENV_HOME, str(bundled_bin))
    monkeypatch.setattr(ffmpeg_locator.shutil, "which", lambda _exe: None)

    assert ffmpeg_locator.find_ffprobe() == str(bundled_bin / exe)


def test_falls_back_to_path(monkeypatch) -> None:
    # 封闭候选目录：仓库可能自带 bin/ffmpeg，PATH 兜底语义必须在
    # 无候选目录的环境下验证（真实候选目录由独立用例检查）。
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    monkeypatch.setattr(ffmpeg_locator, "candidate_dirs", lambda: [])
    monkeypatch.setattr(ffmpeg_locator.shutil, "which", lambda name: f"/path/to/{name}")
    assert ffmpeg_locator.find_ffmpeg() == f"/path/to/{exe}"


def test_returns_none_when_nowhere_found(monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_locator.shutil, "which", lambda _exe: None)
    monkeypatch.setattr(ffmpeg_locator, "candidate_dirs", lambda: [])
    assert ffmpeg_locator.find_ffmpeg() is None
    assert ffmpeg_locator.find_ffprobe() is None


def test_result_is_cached_until_cleared(monkeypatch) -> None:
    # 同上：封闭候选目录，避免仓库自带 bin/ffmpeg 抢先命中。
    monkeypatch.setattr(ffmpeg_locator, "candidate_dirs", lambda: [])
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    calls = []

    def fake_which(name: str):
        calls.append(name)
        return f"/path/to/{name}"

    monkeypatch.setattr(ffmpeg_locator.shutil, "which", fake_which)

    assert ffmpeg_locator.find_ffmpeg() == f"/path/to/{exe}"
    assert ffmpeg_locator.find_ffmpeg() == f"/path/to/{exe}"
    assert calls == [exe]  # 第二次命中缓存

    ffmpeg_locator.clear_cache()
    assert ffmpeg_locator.find_ffmpeg() == f"/path/to/{exe}"
    assert calls == [exe, exe]  # 缓存清空后重新解析


def test_repo_root_bin_candidate_resolves() -> None:
    """仓库根 bin/ 候选必须指向仓库根（回归：此前解析到 src/）。"""
    from pathlib import Path

    candidates = ffmpeg_locator.candidate_dirs()
    repo_root = Path(ffmpeg_locator.__file__).resolve().parents[2]
    assert repo_root in candidates
    assert repo_root / "bin" in candidates
