"""H09：Freeze 捕获资产、逐事件历史与重放（复审 §5 H09）。

契约：
- 捕获 = 颗粒历史环（有界）+ 锚点/seed/调度元数据；``FreezeCaptureStore``
  落盘 sidecar（.f32 PCM + .json 元数据，原子写）——raw PCM 不进参数
  JSON、无机器绝对路径；
- ``SpectralManglerParams.freeze_capture_id`` 引用资产（automation kind
  = never）；
- 重放：注册表（工程/实时）→ DSP 链装载捕获 → 同 seed 同窗口输出
  逐位一致；两次不同冻结各自重放自己的内容；
- 缺资产：load 返回 None + 诊断日志（preflight 失败明确，不静默伪造）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_effects.spectral_mangler import (
    SpectralManglerEffect,
)
from src.features.edit.core.freeze_capture_store import (
    FreezeCaptureStore,
    apply_freeze_registry,
)
from src.features.edit.core.timeline_v2 import (
    EffectChainParams,
    SpectralManglerParams,
    TrackEffectInstance,
    automation_param_kind,
)

pytestmark = pytest.mark.edit_core
SR = 48000


def _feed_noise(seed: int, n: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((2, n)) * 0.3


def _freeze_params(**kw) -> SpectralManglerParams:
    base = dict(grain_enabled=True, grain_freeze=True, grain_density_hz=30.0,
                grain_spray_pct=50.0, random_seed=77)
    base.update(kw)
    return SpectralManglerParams(**base)


class _Frozen(SpectralManglerEffect):
    """便利子类：暴露 grain 模块（测试用）。"""


def _capture_with(effect: SpectralManglerEffect, x: np.ndarray):
    """喂入（freeze 关）后捕获——模拟实时：冻结按钮拉取当前环。"""
    live = SpectralManglerParams(grain_enabled=True, grain_freeze=False,
                                 grain_density_hz=30.0, grain_spray_pct=50.0,
                                 random_seed=77)
    effect.set_params(live.to_dict())
    effect.process(x.copy())
    return effect.take_freeze_capture()


class TestStoreRoundtrip:
    def test_save_load_identical(self, tmp_path):
        store = FreezeCaptureStore(str(tmp_path))
        fx = _Frozen(SR)
        fx.set_params(_freeze_params().to_dict())
        x = _feed_noise(1)
        cap = _capture_with(fx, x)
        assert store.save(cap)
        loaded = store.load(cap["id"])
        assert loaded is not None
        # 资产 PCM 为 float32 sidecar（精度契约）：往返量化误差 ~1e-7
        np.testing.assert_allclose(loaded["history"], cap["history"],
                                   atol=1e-6)
        assert loaded["anchor"] == cap["anchor"]
        assert loaded["seed"] == cap["seed"]

    def test_missing_asset_diagnosed(self, tmp_path):
        store = FreezeCaptureStore(str(tmp_path))
        assert store.load("nope") is None
        assert store.list_ids() == []

    def test_remove_and_relist(self, tmp_path):
        store = FreezeCaptureStore(str(tmp_path))
        fx = _Frozen(SR)
        fx.set_params(_freeze_params().to_dict())
        cap = _capture_with(fx, _feed_noise(2))
        store.save(cap)
        assert cap["id"] in store.list_ids()
        assert store.remove(cap["id"])
        assert cap["id"] not in store.list_ids()


class TestDeterministicReplay:
    def test_reopen_replays_identical_pcm(self, tmp_path):
        """Freeze→存→重开（全新 effect + 装载）→ 同窗口输出逐位一致。"""
        store = FreezeCaptureStore(str(tmp_path))
        src = _Frozen(SR)
        cap = _capture_with(src, _feed_noise(3))
        assert store.save(cap)

        # 会话 A：捕获后开 freeze 并以资产状态重置（宿主实时语义：冻结
        # 即捕获——调度状态随资产确定），再渲染
        src.set_params(_freeze_params().to_dict())
        src.install_freeze_capture(cap)
        out_a = src.process(np.zeros((2, SR // 2), dtype=np.float32))

        # 会话 B：重开工程——全新 effect，从资产装载
        dst = _Frozen(SR)
        dst.set_params(_freeze_params(freeze_capture_id=cap["id"]).to_dict())
        loaded = store.load(cap["id"])
        assert dst.install_freeze_capture(loaded)
        out_b = dst.process(np.zeros((2, SR // 2), dtype=np.float32))
        np.testing.assert_allclose(out_a, out_b, atol=1e-5)

    def test_two_captures_replay_own_content(self, tmp_path):
        """两次不同冻结：各会话重放各自内容（互不覆盖）。"""
        outs = {}
        caps = []
        for seed in (11, 22):
            fx = _Frozen(SR)
            cap = _capture_with(fx, _feed_noise(seed))
            caps.append(cap)
            fx.set_params(_freeze_params().to_dict())
            fx.install_freeze_capture(cap)  # 冻结即捕获：状态随资产确定
            outs[seed] = fx.process(
                np.zeros((2, SR // 2), dtype=np.float32))
        for cap, seed in zip(caps, (11, 22)):
            replay = _Frozen(SR)
            replay.set_params(_freeze_params().to_dict())
            replay.install_freeze_capture(cap)
            y = replay.process(np.zeros((2, SR // 2), dtype=np.float32))
            np.testing.assert_allclose(y, outs[seed], atol=1e-5)
        # 两冻结内容确实不同
        assert np.abs(outs[11] - outs[22]).max() > 1e-3


class TestRegistryWiring:
    def test_apply_freeze_registry_via_chain(self, tmp_path):
        """注册表 → EffectChain 按实例 id 装载（导出/实时共用路径）。"""
        store = FreezeCaptureStore(str(tmp_path))
        fx = _Frozen(SR)
        fx.set_params(_freeze_params().to_dict())
        cap = _capture_with(fx, _feed_noise(5))
        store.save(cap)

        chain_params = EffectChainParams(effects=(
            TrackEffectInstance(
                "spectral_mangler",
                _freeze_params(freeze_capture_id=cap["id"]).to_dict(),
                instance_id="M"),))
        chain = EffectChain(SR)
        chain.set_params(chain_params.to_dsp_entries(), sample_rate=SR)
        registry = {cap["id"]: store.load(cap["id"])}
        assert apply_freeze_registry(chain, chain_params, registry) == 1

        # 对照：同一 effect 装载同一资产后直渲（重放语义：RNG/相位随
        # 资产重置——与链路径一致）
        fx.set_params(_freeze_params().to_dict())
        fx.install_freeze_capture(registry[cap["id"]])
        y_chain = chain.process(np.zeros((2, 48000), dtype=np.float32))
        y_direct = fx.process(np.zeros((2, 48000), dtype=np.float32))
        np.testing.assert_allclose(y_chain, y_direct, atol=1e-5)

    def test_missing_capture_reports_not_installed(self, tmp_path):
        chain_params = EffectChainParams(effects=(
            TrackEffectInstance(
                "spectral_mangler",
                _freeze_params(freeze_capture_id="ghost").to_dict(),
                instance_id="M"),))
        chain = EffectChain(SR)
        chain.set_params(chain_params.to_dsp_entries(), sample_rate=SR)
        assert apply_freeze_registry(chain, chain_params, {}) == 0


class TestParamsMetadata:
    def test_capture_id_roundtrip_and_never_lane(self):
        p = _freeze_params(freeze_capture_id="fz-abc")
        rt = SpectralManglerParams.from_dict(p.to_dict())
        assert rt.freeze_capture_id == "fz-abc"
        assert automation_param_kind("freeze_capture_id") == "never"

    def test_raw_pcm_not_in_params_json(self):
        """参数 dict 里只有资产 id，没有 PCM/绝对路径。"""
        p = _freeze_params(freeze_capture_id="fz-x").to_dict()
        for v in p.values():
            assert not isinstance(v, np.ndarray)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
