"""§3.3 预渲染 PCM 缓存：规格哈希、原子替换、取消清理、保护/淘汰。

验收来自 docs/plans/2026-11-18-clip-editor-and-mixer-roadmap.md §3：
- 缓存按规格哈希索引（工作采样率、源指纹、绑定、输入范围、有序 steps）；
- 临时文件 + 校验 + 原子替换；取消/失败清掉临时文件；
- 回收前保护正在使用的缓存；损坏条目按未命中处理并重建；
- 预渲染基底切块应用与整段渲染逐样本一致（播放 == 试听 == 导出）。
"""
from __future__ import annotations

import threading
from dataclasses import replace

import numpy as np
import pytest

from src.features.edit.core import pcm_render_cache as prc
from src.features.edit.core.audio_edit import (
    append_step,
    apply_clip_audio_edit_block,
    inline_audio_edit,
    make_step,
    new_audio_edit,
    pre_render_steps,
    render_pre_render_steps,
    replace_processor_step,
    requires_pre_render,
    validate_audio_edit,
)
from src.features.edit.core.pcm_render_cache import (
    PreRenderCancelled,
    PreRenderSpec,
    PcmRenderCache,
)

RATE = 48000


def _spec(fingerprint: str = "abc", steps_sig: tuple = (("denoise", 12.0),),
          channels: int = 2, samples: int = 4800) -> PreRenderSpec:
    return PreRenderSpec(
        source_fingerprint=fingerprint, sample_rate=RATE,
        channel_count=channels, total_samples=samples,
        steps_signature=steps_sig)


def _pcm(channels: int = 2, samples: int = 4800) -> np.ndarray:
    rng = np.random.default_rng(20261118)
    return rng.standard_normal((channels, samples)).astype(np.float32) * 0.1


# ----------------------------------------------------------------------
# 规格哈希
# ----------------------------------------------------------------------
def test_spec_key_is_stable_and_sensitive():
    base = _spec().key
    assert base == _spec().key  # 稳定
    assert base != _spec(fingerprint="other").key
    assert base != _spec(steps_sig=(("denoise", 6.0),)).key
    assert base != _spec(channels=1).key
    assert base != _spec(samples=4801).key


# ----------------------------------------------------------------------
# 构建 / 原子替换 / 取消与失败清理
# ----------------------------------------------------------------------
def test_build_stores_and_looks_up(tmp_path):
    cache = PcmRenderCache(tmp_path)
    pcm = _pcm()
    out = cache.build(_spec(), lambda: pcm)
    assert out is pcm
    # 磁盘 + 元数据就位；无临时文件残留。
    key = _spec().key
    assert (tmp_path / f"{key}.npz").is_file()
    assert (tmp_path / f"{key}.json").is_file()
    assert list(tmp_path.glob("*.tmp*")) == []
    # 新实例（空内存）从磁盘读回且逐样本一致。
    fresh = PcmRenderCache(tmp_path)
    loaded = fresh.lookup(_spec())
    assert loaded is not None
    np.testing.assert_array_equal(loaded, pcm)


def test_build_is_idempotent_on_hit(tmp_path):
    cache = PcmRenderCache(tmp_path)
    calls = []
    cache.build(_spec(), lambda: (calls.append(1), _pcm())[1])
    cache.build(_spec(), lambda: (_ for _ in ()).throw(AssertionError("rebuild")))
    assert calls == [1]


def test_cancelled_build_leaves_no_artifacts(tmp_path):
    cache = PcmRenderCache(tmp_path)
    with pytest.raises(PreRenderCancelled):
        cache.build(_spec(), lambda: _pcm(), cancel_check=lambda: True)
    assert list(tmp_path.glob("*")) == []


def test_failed_builder_leaves_no_temp_files(tmp_path):
    cache = PcmRenderCache(tmp_path)

    def _boom():
        raise RuntimeError("decode failed")

    with pytest.raises(RuntimeError):
        cache.build(_spec(), _boom)
    assert list(tmp_path.glob("*.tmp*")) == []
    assert cache.lookup(_spec()) is None


def test_payload_mismatch_is_rejected(tmp_path):
    cache = PcmRenderCache(tmp_path)
    with pytest.raises(ValueError):
        cache.build(_spec(), lambda: _pcm(samples=10))  # 形状与 spec 不符


def test_corrupt_entry_is_miss_and_removed(tmp_path):
    cache = PcmRenderCache(tmp_path)
    cache.build(_spec(), lambda: _pcm())
    key = _spec().key
    (tmp_path / f"{key}.npz").write_bytes(b"not an npz")
    # 新实例（内存为空）从磁盘读——损坏条目按未命中处理并删除。
    fresh = PcmRenderCache(tmp_path)
    assert fresh.lookup(_spec()) is None
    assert not (tmp_path / f"{key}.npz").exists()
    fresh.build(_spec(), lambda: _pcm())
    assert PcmRenderCache(tmp_path).lookup(_spec()) is not None


def test_tampered_meta_is_miss(tmp_path):
    cache = PcmRenderCache(tmp_path)
    cache.build(_spec(), lambda: _pcm())
    key = _spec().key
    meta_path = tmp_path / f"{key}.json"
    meta_path.write_text('{"schema": 999, "key": "%s"}' % key)
    assert PcmRenderCache(tmp_path).lookup(_spec()) is None


def test_missing_meta_does_not_delete_inflight_pcm_archive(tmp_path):
    cache = PcmRenderCache(tmp_path)
    spec = _spec()
    cache.store(spec, _pcm())
    archive = tmp_path / f"{spec.key}.npz"
    (tmp_path / f"{spec.key}.json").unlink()
    assert PcmRenderCache(tmp_path).lookup(spec) is None
    assert archive.is_file()


# ----------------------------------------------------------------------
# 并发去重 / 后台构建 / 监听
# ----------------------------------------------------------------------
def test_get_or_build_deduplicates_concurrent_calls(tmp_path):
    cache = PcmRenderCache(tmp_path)
    calls = []
    started = threading.Event()
    release = threading.Event()

    def _slow_builder():
        calls.append(1)
        started.set()
        release.wait(2.0)
        return _pcm()

    results = {}

    def _worker():
        results[threading.get_ident()] = cache.get_or_build(_spec(), _slow_builder)

    thread = threading.Thread(target=_worker)
    thread.start()
    assert started.wait(2.0)
    main = cache.get_or_build(_spec(), _slow_builder)  # 应等待而非重复构建
    release.set()
    thread.join(2.0)
    assert calls == [1]
    np.testing.assert_array_equal(main, results[thread.ident])


def test_get_or_build_propagates_error_to_waiter(tmp_path):
    cache = PcmRenderCache(tmp_path)
    started = threading.Event()
    release = threading.Event()
    errors = {}

    def _boom():
        started.set()
        release.wait(2.0)
        raise RuntimeError("no dsp")

    def _owner():
        try:
            cache.get_or_build(_spec(), _boom)
        except RuntimeError as exc:
            errors["owner"] = exc

    thread = threading.Thread(target=_owner)
    thread.start()
    assert started.wait(2.0)
    with pytest.raises(RuntimeError):
        cache.get_or_build(_spec(), _boom)
    release.set()
    thread.join(2.0)
    assert "owner" in errors


def test_spawn_build_notifies_listener(tmp_path):
    cache = PcmRenderCache(tmp_path)
    done = threading.Event()
    keys = []
    cache.add_listener(lambda key: (keys.append(key), done.set()))
    assert cache.spawn_build(_spec(), lambda: _pcm())
    assert done.wait(5.0)
    assert keys == [_spec().key]
    assert cache.lookup(_spec()) is not None


def test_spawn_build_dedupes_inflight(tmp_path):
    cache = PcmRenderCache(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def _slow():
        started.set()
        release.wait(2.0)
        return _pcm()

    assert cache.spawn_build(_spec(), _slow)
    assert started.wait(2.0)
    assert not cache.spawn_build(_spec(), _slow)  # 在途 → 不重复
    release.set()
    # 等待后台线程落盘。
    for _ in range(200):
        if cache.lookup(_spec()) is not None:
            break
        threading.Event().wait(0.02)
    assert cache.lookup(_spec()) is not None


# ----------------------------------------------------------------------
# 保护 / 回收
# ----------------------------------------------------------------------
def test_memory_lru_evicts_oldest_unprotected(tmp_path):
    cache = PcmRenderCache(tmp_path, memory_capacity=2)
    spec_a, spec_b, spec_c = _spec(samples=100), _spec(samples=200), _spec(samples=300)
    with cache.protect(spec_a):
        cache.store(spec_a, _pcm(samples=100))
        cache.store(spec_b, _pcm(samples=200))
        cache.store(spec_c, _pcm(samples=300))
        # 保护期内 A 驻留内存；B 被淘汰（磁盘仍可读回）。
        assert cache.lookup(spec_a) is not None
    cache.store(_spec(samples=400), _pcm(samples=400))
    # 磁盘条目仍可重建（回收只是内存驱逐）。
    assert cache.lookup(spec_b) is not None


def test_memory_byte_limit_reloads_from_disk_without_evicting_pinned_entries(tmp_path):
    small, fits, oversized = _spec(samples=50), _spec(samples=75), _spec(samples=150)
    cache = PcmRenderCache(tmp_path, memory_capacity=4, memory_max_bytes=800)
    with cache.protect(small):
        cache.store(small, _pcm(samples=50))
        cache.store(fits, _pcm(samples=75))
        assert set(cache._memory) == {small.key}
        assert cache._memory_bytes == 400
        np.testing.assert_array_equal(cache.lookup(fits), _pcm(samples=75))
    np.testing.assert_array_equal(cache.lookup(fits), _pcm(samples=75))
    assert set(cache._memory) == {fits.key}
    assert cache._memory_bytes == 600

    cache.store(oversized, _pcm(samples=150))
    np.testing.assert_array_equal(cache.lookup(oversized), _pcm(samples=150))
    assert set(cache._memory) == {fits.key}
    assert cache._memory_bytes == 600


def test_memory_only_oversize_result_remains_available_to_existing_waiters():
    spec = _spec(samples=100)
    cache = PcmRenderCache(memory_max_bytes=400)
    pcm = _pcm(samples=100)
    assert not cache.can_store(spec)
    assert not cache.spawn_build(spec, lambda: pytest.fail("cannot cache this result"))

    started = threading.Event()
    waiter_entered = threading.Event()
    release = threading.Event()
    values: list[np.ndarray] = []

    def build() -> np.ndarray:
        started.set()
        assert release.wait(2.0)
        return pcm

    def use_builder() -> None:
        values.append(cache.get_or_build(spec, build))

    owner = threading.Thread(target=use_builder)
    owner.start()
    assert started.wait(2.0)
    # Force overlap at the actual wait instead of sleep-based scheduling.
    wait = cache._inflight[spec.key].event.wait

    def mark_wait() -> bool:
        waiter_entered.set()
        return wait(2.0)

    cache._inflight[spec.key].event.wait = mark_wait
    waiter = threading.Thread(target=use_builder)
    waiter.start()
    try:
        assert waiter_entered.wait(2.0)
    finally:
        release.set()
        owner.join(2.0)
        waiter.join(2.0)
    assert len(values) == 2 and all(value is pcm for value in values)
    assert cache.lookup(spec) is None and cache._memory_bytes == 0


def test_trim_disk_respects_protection(tmp_path):
    cache = PcmRenderCache(tmp_path, disk_max_entries=1, disk_max_bytes=10 ** 9)
    spec_a = _spec(samples=100)   # 最旧，但被保护
    spec_b = _spec(samples=200)   # 中间，未保护 → 应被回收
    spec_c = _spec(samples=300)   # 最新 → 保留
    with cache.protect(spec_a):
        cache.store(spec_a, _pcm(samples=100))
        cache.store(spec_b, _pcm(samples=200))
        cache.store(spec_c, _pcm(samples=300))
        cache.trim_disk()
        assert (tmp_path / f"{spec_a.key}.npz").exists()
        assert (tmp_path / f"{spec_c.key}.npz").exists()
        assert not (tmp_path / f"{spec_b.key}.npz").exists()


def test_clear_removes_files(tmp_path):
    cache = PcmRenderCache(tmp_path)
    cache.build(_spec(), lambda: _pcm())
    cache.clear()
    assert cache.lookup(_spec()) is None
    assert list(tmp_path.glob("*.npz")) == []


# ----------------------------------------------------------------------
# spectral_denoise step：校验 / 分派 / 整段渲染
# ----------------------------------------------------------------------
def _denoise_edit(target: tuple[int, int], noise: tuple[int, int],
                  reduction_db: float = 12.0, nfft: int = 2048) -> dict:
    return new_audio_edit(steps=[make_step(
        "spectral_denoise", target[0], target[1], (0, 1),
        reduction_db=reduction_db,
        noise_start=noise[0], noise_end=noise[1], nfft=nfft,
    )])


def test_spectral_denoise_step_validation():
    ok = _denoise_edit((RATE, 4 * RATE), (0, RATE))
    assert validate_audio_edit(ok, clip_output_samples=4 * RATE) == []
    bad = [
        _denoise_edit((RATE, 4 * RATE), (0, 100)),          # 采集过短
        _denoise_edit((RATE, 4 * RATE), (0, RATE), nfft=123),  # 非法 FFT
        _denoise_edit((RATE, 4 * RATE), (0, RATE), reduction_db=99.0),
        _denoise_edit((RATE, 4 * RATE), (RATE + 1, 5 * RATE)),  # 越界
    ]
    for edit in bad:
        assert validate_audio_edit(edit, clip_output_samples=4 * RATE), edit


def test_requires_pre_render_and_inline_split():
    edit = append_step(
        _denoise_edit((RATE, 2 * RATE), (0, RATE)),
        make_step("range_gain", 0, RATE, (0,), gain_db=-6.0))
    assert requires_pre_render(edit)
    assert len(pre_render_steps(edit)) == 1
    inline = inline_audio_edit(edit)
    assert not requires_pre_render(inline)
    assert [s["processor_id"] for s in inline["steps"]] == ["range_gain"]
    assert not requires_pre_render(None)
    assert inline_audio_edit(None) is None


def test_replace_processor_step_is_idempotent():
    edit = None
    for reduction in (12.0, 6.0):
        edit = replace_processor_step(
            edit, "spectral_denoise",
            make_step("spectral_denoise", 0, RATE, (0, 1),
                      reduction_db=reduction, noise_start=0, noise_end=RATE))
    denoise_steps = [s for s in edit["steps"]
                     if s["processor_id"] == "spectral_denoise"]
    assert len(denoise_steps) == 1
    assert denoise_steps[0]["params"]["reduction_db"] == 6.0


def _noisy_pcm() -> np.ndarray:
    """1 s 噪声（采集段）+ 1 s 440 Hz+噪声（处理段）。"""
    rng = np.random.default_rng(20261118)
    noise = (rng.standard_normal((2, RATE)) * 0.01).astype(np.float32)
    t = np.arange(RATE) / RATE
    tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    signal = tone + (rng.standard_normal((2, RATE)) * 0.01).astype(np.float32)
    return np.concatenate([noise, signal], axis=1)


def test_render_pre_render_steps_matches_direct_denoise():
    from src.features.edit.core.audio_denoise import denoise_spectral_subtraction
    pcm = _noisy_pcm()
    edit = _denoise_edit((RATE, 2 * RATE), (0, RATE), reduction_db=12.0)
    rendered = render_pre_render_steps(pcm, edit, sample_rate=RATE)
    direct = denoise_spectral_subtraction(
        pcm, RATE, (0, RATE), (RATE, 2 * RATE), 12.0)
    np.testing.assert_array_equal(rendered, direct)
    # 不修改输入
    np.testing.assert_array_equal(pcm, _noisy_pcm())


def test_render_pre_render_steps_requires_scipy(monkeypatch):
    import src.features.edit.core.audio_denoise as denoise_mod
    monkeypatch.setattr(denoise_mod, "_SCIPY_AVAILABLE", False)
    with pytest.raises(RuntimeError):
        render_pre_render_steps(
            _noisy_pcm(), _denoise_edit((RATE, 2 * RATE), (0, RATE)),
            sample_rate=RATE)


# ----------------------------------------------------------------------
# 块应用：基底切片 == 整段（任意分块）
# ----------------------------------------------------------------------
def test_block_applier_with_base_matches_whole_array():
    pcm = _noisy_pcm()
    edit = append_step(
        _denoise_edit((RATE, 2 * RATE), (0, RATE)),
        make_step("range_gain", 0, RATE, (0,), gain_db=-6.0))
    base = render_pre_render_steps(pcm, edit, sample_rate=RATE)
    inline = inline_audio_edit(edit)
    whole = apply_clip_audio_edit_block(
        base[:, :4800], 0, edit, sample_rate=RATE, pre_render_base=base)
    # 任意奇数分块拼接与整段逐样本一致。
    block_starts = [0, 4800, 4801, 12000, 20001]
    collected = []
    position = 0
    for start in block_starts:
        count = 997
        piece = apply_clip_audio_edit_block(
            base[:, start:start + count], start, edit,
            sample_rate=RATE, pre_render_base=base)
        assert piece.shape == (2, count)
        collected.append(piece)
        position += count
    stitched = np.concatenate(collected, axis=1)
    reference_parts = []
    for start in block_starts:
        reference_parts.append(apply_clip_audio_edit_block(
            base[:, start:start + 997], start, edit,
            sample_rate=RATE, pre_render_base=base))
    np.testing.assert_array_equal(
        stitched, np.concatenate(reference_parts, axis=1))
    # inline 步骤确实作用在基底之上（非恒等）。
    assert not np.allclose(whole, base[:, :4800])
    del inline, position


def test_block_applier_falls_back_to_inline_when_base_missing():
    pcm = _noisy_pcm()
    edit = append_step(
        _denoise_edit((RATE, 2 * RATE), (0, RATE)),
        make_step("range_gain", RATE, 2 * RATE, (0,), gain_db=-20.0))
    block = pcm[:, RATE:2 * RATE]
    # 无基底：临时回退——inline 生效、降噪缺席。
    fallback = apply_clip_audio_edit_block(
        block, RATE, edit, sample_rate=RATE, pre_render_base=None)
    expected = apply_clip_audio_edit_block(
        block, RATE, inline_audio_edit(edit), sample_rate=RATE,
        pre_render_base=None)
    np.testing.assert_array_equal(fallback, expected)
    # 与有基底的版本不同（降噪在场）。
    base = render_pre_render_steps(pcm, edit, sample_rate=RATE)
    with_base = apply_clip_audio_edit_block(
        block, RATE, edit, sample_rate=RATE, pre_render_base=base)
    assert not np.allclose(fallback, with_base)


def test_block_applier_zero_pads_past_base_end():
    base = _pcm(samples=100)
    edit = _denoise_edit((0, 100), (0, 50))
    # 块超出基底尾端（帧↔样本舍入）→ 补零保持块长。
    block = apply_clip_audio_edit_block(
        np.zeros((2, 40), dtype=np.float32), 80, edit,
        sample_rate=RATE, pre_render_base=base)
    assert block.shape == (2, 40)
    np.testing.assert_array_equal(block[:, 20:], np.zeros((2, 20), dtype=np.float32))
    np.testing.assert_array_equal(block[:, :20], base[:, 80:100])


# ----------------------------------------------------------------------
# 计划解析（时间线片段 → 预渲染计划）
# ----------------------------------------------------------------------
def _plan_fixture(tmp_path, *, audio_edit=None, speed=None):
    """最小稳定模型（duck-typed）：真实 WAV 路径 + 2 s 立体声内容。"""
    import wave
    from types import SimpleNamespace

    wav_path = tmp_path / "source.wav"
    t = np.arange(2 * RATE) / RATE
    stereo = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1)
    pcm16 = (stereo * 32000).astype("<i2")
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm16.tobytes())

    asset = SimpleNamespace(
        asset_id="asset-1", path=str(wav_path), fps=24.0,
        audio_channels=2, audio_channel_layout="stereo",
        duration_seconds=2.0)
    project = SimpleNamespace(assets={"asset-1": asset})
    timeline = SimpleNamespace(fps=24.0, timeline_id="t1", revision=3)
    clip = SimpleNamespace(
        clip_id="c1", asset_id="asset-1", source_start=0, source_duration=48,
        timeline_duration=48, timeline_start=0, source_sample_offset=0,
        is_reverse_playback=lambda: False, effective_speed=speed or 1.0,
        audio_edit=audio_edit)
    return project, timeline, clip, asset


def test_build_pre_render_plan_none_without_pre_render_steps(tmp_path):
    project, timeline, clip, asset = _plan_fixture(tmp_path)
    assert prc.build_pre_render_plan(project, timeline, clip, asset) is None


def test_build_pre_render_plan_range_and_spec(tmp_path):
    project, timeline, clip, asset = _plan_fixture(
        tmp_path, audio_edit=_denoise_edit((0, 48000), (0, 48000)))
    plan = prc.build_pre_render_plan(project, timeline, clip, asset)
    assert plan is not None and plan.blocked_reason is None
    assert plan.total_samples == pytest.approx(96000, abs=4)  # 2 s @48k
    spec = plan.spec()
    assert spec.channel_count == 2 and spec.sample_rate == RATE
    # 步骤签名参与 key：参数变化 → 不同缓存条目。
    other = prc.build_pre_render_plan(
        project, timeline, clip, asset,
    )
    assert other.spec().key == spec.key


def test_same_source_different_in_points_use_different_pcm_cache_entries(tmp_path) -> None:
    project, timeline, clip, asset = _plan_fixture(
        tmp_path, audio_edit=_denoise_edit((0, RATE), (0, RATE // 2)))
    original = prc.build_pre_render_plan(project, timeline, clip, asset)
    first = replace(original, end_sample=RATE)
    second = replace(first, start_sample=1, end_sample=RATE + 1)
    cache = PcmRenderCache(tmp_path / "renders")
    cached_first = cache.build(first.spec(), first.builder())
    cached_second = cache.build(second.spec(), second.builder())
    assert first.spec().key != second.spec().key
    assert first.spec().key != replace(first, reverse=True).spec().key
    assert not np.array_equal(cached_first, cached_second)
    np.testing.assert_array_equal(cached_second, second.builder()())


def test_build_pre_render_plan_blocks_retimed(tmp_path):
    project, timeline, clip, asset = _plan_fixture(
        tmp_path, audio_edit=_denoise_edit((0, 48000), (0, 48000)), speed=2.0)
    plan = prc.build_pre_render_plan(project, timeline, clip, asset)
    assert plan is not None and plan.blocked_reason
    assert "retimed" in plan.blocked_reason


def test_plan_builder_renders_denoised_base(tmp_path):
    """计划 builder：解码 + 降噪整段 == 直接对解码 PCM 跑 render 步骤。"""
    from src.features.edit.core.audio_edit import render_pre_render_steps as _rpr
    from src.features.edit.core.clip_pcm import decode_audio_range
    project, timeline, clip, asset = _plan_fixture(
        tmp_path, audio_edit=_denoise_edit((0, 96000), (0, 48000)))
    plan = prc.build_pre_render_plan(project, timeline, clip, asset)
    raw = decode_audio_range(plan.source_path, 0, 96000, RATE)
    expected = _rpr(raw, {"steps": list(plan.steps)}, sample_rate=RATE)
    built = plan.builder()()
    np.testing.assert_allclose(built, expected, atol=1e-6)
    assert built.shape == (2, 96000)


def test_preflight_pre_render_steps_reports_blockers(tmp_path):
    from types import SimpleNamespace
    empty = SimpleNamespace(fps=24.0, audio_tracks=())
    assert prc.preflight_pre_render_steps(empty) == ()
    assert prc.preflight_pre_render_steps(None) == ()

    clip = SimpleNamespace(
        clip_id="c1", timeline_duration=48, effective_speed=2.0,
        is_reverse_playback=lambda: False,
        audio_edit=_denoise_edit((0, 48000), (0, 48000)))
    track = SimpleNamespace(clips=[clip])
    sequence = SimpleNamespace(fps=24.0, audio_tracks=[track])
    problems = prc.preflight_pre_render_steps(sequence)
    assert any("变速" in message for message in problems)

    still = SimpleNamespace(
        clip_id="c2", timeline_duration=48, effective_speed=1.0,
        is_reverse_playback=lambda: False,
        audio_edit=_denoise_edit((0, 48000), (0, 48000)))
    clean = SimpleNamespace(fps=24.0, audio_tracks=[
        SimpleNamespace(clips=[still])])
    assert prc.preflight_pre_render_steps(clean) == ()


# ----------------------------------------------------------------------
# 实时路径（AudioMixer.mix_samples）：预渲染基底逐块出声
# ----------------------------------------------------------------------
def test_playback_mix_uses_pre_render_base(qapp):
    """含 spectral_denoise 的栈：mix_samples 输出 == 缓存基底切片。"""
    from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState

    pcm = _noisy_pcm()  # 2 s：1 s 噪声 + 1 s 440 Hz+噪声
    edit = _denoise_edit((RATE, 2 * RATE), (0, RATE))
    base = render_pre_render_steps(pcm, edit, sample_rate=RATE)

    class _ClipDecoder:
        _timeline_start = 0

        def __init__(self, source, base_pcm, audio_edit, pre_base):
            self._source = source
            self._audio_edit = audio_edit
            self._pre_render_base = pre_base

        def decode_samples_for_frame(self, frame, fps, num_samples):
            start = int(round(frame * RATE / float(fps)))
            block = self._source[:, start:start + num_samples]
            if block.shape[1] < num_samples:
                block = np.pad(block, ((0, 0), (num_samples - block.shape[1], 0)))
            return block.copy()

        def close(self):
            pass

    mixer = AudioMixer()
    mixer.add_track("a1", _ClipDecoder(pcm, base, edit, base),
                    AudioTrackState(track_id="a1", volume=1.0))
    # 第 24 帧起 → 片段局部样本 48000 起（降噪目标区内）。
    out, _ = mixer.mix_samples(24, 24.0, RATE, 2)
    # 轨音量 1.0、无 clip 增益/淡化 → 输出即基底切片（降噪在场）。
    np.testing.assert_allclose(out, base[:, RATE:2 * RATE], atol=1e-6)
    assert not np.allclose(out, pcm[:, RATE:2 * RATE])
    mixer.remove_track("a1")

    # 基地缺席（缓存构建中）：同一 mix 回退 inline-only（降噪缺席）。
    fallback_mixer = AudioMixer()
    fallback_mixer.add_track("a1", _ClipDecoder(pcm, base, edit, None),
                             AudioTrackState(track_id="a1", volume=1.0))
    out_fallback, _ = fallback_mixer.mix_samples(24, 24.0, RATE, 2)
    np.testing.assert_allclose(out_fallback, pcm[:, RATE:2 * RATE], atol=1e-6)
    assert not np.allclose(out_fallback, base[:, RATE:2 * RATE])


@pytest.mark.parametrize("processor", ["de_click", "de_hum"])
def test_updated_restoration_rebuilds_pre_render_data_for_saved_recipes(processor: str) -> None:
    from dataclasses import replace

    from src.features.edit.core.audio_edit import make_step
    from src.features.edit.core.pcm_render_cache import PreRenderBuildPlan

    step = make_step(processor, 0, 4800, (0,))
    plan = PreRenderBuildPlan("source.wav", 0, 4800, 48000, "source", (step,), expected_channels=1)
    current = plan.spec()
    signature = current.steps_signature[0]
    old = replace(current, steps_signature=((signature[0], signature[1],
                                            step["algorithm_version"], *signature[3:]),))
    assert current.key != old.key
    assert step["algorithm_version"] == 1
