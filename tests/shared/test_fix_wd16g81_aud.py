"""wd16-g81 批 D 修复验证（REMEDIATION_PLAN 18 条，shared/ai_models 下半 + assets）。

覆盖条目：AUD-31621 / 31623 / 31700 / 31701 / 31702 / 31703 / 31704 / 31705 /
31800 / 31801 / 31802 / 31803 / 31804 / 31809 / 31816 / 32001 / 32002 / 32003。

网络、ONNX、keychain 全部 mock：不发网络请求、不读真实模型权重、不触碰
用户 Keychain。与 tests/edit/ai_editing/test_volcengine_generation_provider.py
（正向 wire 契约）、tests/shared/test_fix_wd15g80_aud.py（批 315/316 上半）
互补，不重复既有断言。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import sys

import pytest

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# 公共测试工具
# ---------------------------------------------------------------------------


def _consent(provider_id: str):
    from src.shared.ai_models.generation.generation_models import GenerationConsent

    return GenerationConsent(
        confirmed=True,
        provider_id=provider_id,
        cost_acknowledged=True,
        upload_acknowledged=True,
        copyright_acknowledged=True,
        target_location="generation_bin",
    )


def _request(request_id: str = "req-1", **overrides):
    from src.shared.ai_models.generation.generation_models import (
        GenerationModality,
        GenerationRequest,
    )

    kwargs = dict(
        request_id=request_id,
        modality=GenerationModality.IMAGE,
        prompt="a blue lamp",
        consent=_consent("volcengine-image"),
    )
    kwargs.update(overrides)
    return GenerationRequest(**kwargs)


def _image_profile(**overrides):
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile

    kwargs = dict(
        profile_id="volcengine-image",
        display_name="Seedream",
        provider_kind="volcengine_seedream",
        endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
        model_id="doubao-seedream-5.0-lite",
        credential_ref="topos:image",
        modalities=("image",),
    )
    kwargs.update(overrides)
    return AiProviderProfile(**kwargs)


class _Response:
    """urlopen 风格的最小响应桩。"""

    def __init__(self, payload: bytes, headers: dict | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self._payload


class _RecordingStore:
    """记录读取次数的凭据存储。"""

    def __init__(self, secret: str = "secret") -> None:
        self.secret = secret
        self.get_calls = 0

    def get_secret(self, credential_ref: str) -> str | None:
        self.get_calls += 1
        return self.secret

    def set_secret(self, credential_ref: str, secret: str) -> None:
        raise AssertionError("not expected in these tests")

    def delete_secret(self, credential_ref: str) -> None:
        raise AssertionError("not expected in these tests")


# ---------------------------------------------------------------------------
# AUD-31621：from_dict 非映射 data_policy/limits 不再炸掉整批加载
# ---------------------------------------------------------------------------


class TestAud31621CorruptProfileIsolated:
    def test_from_dict_accepts_non_mapping_data_policy_and_limits(self):
        from src.shared.ai_models.generation.provider_profiles import AiProviderProfile

        profile = AiProviderProfile.from_dict({
            "profile_id": "p1",
            "display_name": "P1",
            "data_policy": "local",   # 非映射：此前 dp.get 抛 AttributeError
            "limits": 42,             # 非映射：此前 lm.get 抛 AttributeError
        })
        assert profile.data_policy.execution == "local"
        assert profile.data_policy.allow_media_upload is False
        assert profile.limits.max_retries == 2  # ProviderLimits 默认值

    def test_registry_skips_corrupted_profile_and_loads_good_one(self):
        from src.shared.ai_models.generation.provider_profiles import ProfileRegistry

        class _FakeConfig:
            def __init__(self, data):
                self._data = data

            def get(self, key):
                return self._data.get(key)

        config = _FakeConfig({
            "ai_editing.profiles": [
                {  # 完好 profile
                    "profile_id": "good",
                    "display_name": "Good",
                },
                {  # 单条损坏：limits 数值字段非法 → ValueError → 跳过该条
                    "profile_id": "bad",
                    "display_name": "Bad",
                    "limits": {"max_retries": "not-a-number"},
                },
                {  # 非映射 data_policy：修复后降级为默认值并成功加载
                    "profile_id": "degraded",
                    "display_name": "Degraded",
                    "data_policy": "local",
                },
            ],
        })
        registry = ProfileRegistry()
        registry.load_from_config(config)
        assert registry.get("good") is not None
        assert registry.get("bad") is None
        degraded = registry.get("degraded")
        assert degraded is not None
        assert degraded.data_policy.execution == "local"  # DataPolicy 默认
        assert degraded.data_policy.allow_media_upload is False

    def test_registry_user_file_skips_corrupted_profile(self, tmp_path):
        from src.shared.ai_models.generation.provider_profiles import ProfileRegistry

        path = tmp_path / "ai_profiles.json"
        path.write_text(json.dumps({
            "profiles": [
                {"profile_id": "good", "display_name": "Good"},
                {"profile_id": "bad", "display_name": "Bad",
                 "limits": {"max_parallel_jobs": "oops"}},
            ],
        }), encoding="utf-8")
        registry = ProfileRegistry()
        assert registry.load_from_user_file(path) is True
        assert registry.get("good") is not None
        assert registry.get("bad") is None


# ---------------------------------------------------------------------------
# AUD-31623：暂时性读取失败不再等同于"文件不存在"
# ---------------------------------------------------------------------------


def _make_path_unreadable(path) -> None:
    """把配置路径变为不可读（W01 平台适配）。

    POSIX：chmod 000。Windows：NTFS 的 chmod(0) 只设置只读属性，读不受
    影响——用同名**目录**占位制造 open() → PermissionError，等价检验
    "读取失败 → strict 抛错/拒绝覆盖"的产品契约。
    """
    if sys.platform == "win32":
        if path.is_file():
            path.unlink()
        path.mkdir()
    else:
        path.chmod(0o000)


def _restore_path(path) -> None:
    if sys.platform == "win32":
        import shutil as _shutil
        if path.is_dir():
            _shutil.rmtree(path)
    else:
        path.chmod(0o600)


class TestAud31623ConfigLoadDistinguishesFailures:
    def test_missing_file_returns_empty_in_both_modes(self, tmp_path):
        from src.shared.ai_models.generation.user_ai_config import load_user_ai_config

        path = tmp_path / "ai_profiles.json"
        assert load_user_ai_config(path) == {}
        assert load_user_ai_config(path, strict=True) == {}

    def test_corrupt_json_strict_raises_non_strict_degrades(self, tmp_path):
        from src.shared.ai_models.generation.user_ai_config import (
            UserAiConfigError,
            load_user_ai_config,
        )

        path = tmp_path / "ai_profiles.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_user_ai_config(path) == {}  # 只读路径降级
        with pytest.raises(UserAiConfigError):
            load_user_ai_config(path, strict=True)

    def test_permission_denied_strict_raises(self, tmp_path):
        from src.shared.ai_models.generation.user_ai_config import (
            UserAiConfigError,
            load_user_ai_config,
        )

        if getattr(os, "geteuid", lambda: -1)() == 0:  # W01: win32 无 geteuid
            pytest.skip("permission bits are irrelevant for root")
        path = tmp_path / "ai_profiles.json"
        path.write_text("{}", encoding="utf-8")
        _make_path_unreadable(path)
        try:
            with pytest.raises(UserAiConfigError):
                load_user_ai_config(path, strict=True)
        finally:
            _restore_path(path)

    def test_profile_save_refuses_to_overwrite_unreadable_config(self, tmp_path):
        """读-改-写调用方（save_to_user_file）在 strict 读取失败时拒绝覆盖。"""
        from src.shared.ai_models.generation.provider_profiles import (
            AiProviderProfile,
            ProfileRegistry,
        )
        from src.shared.ai_models.generation.user_ai_config import UserAiConfigError

        path = tmp_path / "ai_profiles.json"
        original = '{"credentials": {"topos:a": "key-a"}, "profiles": []}'
        path.write_text(original, encoding="utf-8")
        _make_path_unreadable(path)
        registry = ProfileRegistry()
        registry.add(AiProviderProfile(profile_id="new", display_name="New"))
        try:
            with pytest.raises(UserAiConfigError):
                registry.save_to_user_file(path)
        finally:
            _restore_path(path)
        if sys.platform == "win32":
            # 占位目录未被覆盖成文件（等价"原文件未被覆盖"）
            assert not path.is_file()
        else:
            assert path.read_text(encoding="utf-8") == original  # 原文件未被覆盖

    def test_credential_overwrite_refused_when_file_unreadable(self, tmp_path):
        """所有覆盖型调用方共用 save 落盘入口：save 侧守卫兜底（含
        FileCredentialStore 这类未改动的调用方）。"""
        from src.shared.ai_models.generation.credential_store import (
            FileCredentialStore,
        )
        from src.shared.ai_models.generation.user_ai_config import (
            UserAiConfigError,
            save_user_ai_config,
        )

        if getattr(os, "geteuid", lambda: -1)() == 0:  # W01: win32 无 geteuid
            pytest.skip("permission bits are irrelevant for root")
        path = tmp_path / "ai_profiles.json"
        original = '{"credentials": {"topos:a": "key-a"}}'
        path.write_text(original, encoding="utf-8")
        _make_path_unreadable(path)
        store = FileCredentialStore(path)
        try:
            # 加载降级返回 {}（只读容错），但落盘守卫必须拒绝整体覆盖。
            with pytest.raises(UserAiConfigError):
                store.set_secret("topos:b", "key-b")
            # 直接调用 save 也一样被拒。
            with pytest.raises(UserAiConfigError):
                save_user_ai_config({"profiles": []}, path)
        finally:
            _restore_path(path)
        if sys.platform == "win32":
            assert not path.is_file()  # 占位未被覆盖
        else:
            assert json.loads(path.read_text(encoding="utf-8"))["credentials"] == {
                "topos:a": "key-a",
            }

    def test_save_keeps_one_backup_generation(self, tmp_path):
        from src.shared.ai_models.generation.user_ai_config import (
            load_user_ai_config,
            save_user_ai_config,
        )

        path = tmp_path / "ai_profiles.json"
        save_user_ai_config({"generation": 1}, path)
        save_user_ai_config({"generation": 2}, path)

        assert load_user_ai_config(path)["generation"] == 2
        backup = path.with_name(path.name + ".bak")
        assert backup.is_file()
        assert json.loads(backup.read_text(encoding="utf-8"))["generation"] == 1

    def test_save_refuses_to_overwrite_corrupt_existing_file(self, tmp_path):
        from src.shared.ai_models.generation.user_ai_config import (
            UserAiConfigError,
            save_user_ai_config,
        )

        path = tmp_path / "ai_profiles.json"
        path.write_text("{corrupt", encoding="utf-8")
        with pytest.raises(UserAiConfigError):
            save_user_ai_config({"profiles": []}, path)
        assert path.read_text(encoding="utf-8") == "{corrupt"


# ---------------------------------------------------------------------------
# AUD-31700：certifi try 块不再吞 opener 的 OSError 家族异常并重发请求
# ---------------------------------------------------------------------------


class TestAud31700NoDuplicateSend:
    def test_opener_error_is_not_swallowed_and_not_resent(self, monkeypatch):
        import urllib.error
        import urllib.request

        from src.shared.ai_models.generation import volcengine_generation_provider as module

        calls: list[int] = []

        def fake_urlopen(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError("https://x", 500, "boom", None, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        provider = module.VolcengineSeedreamImageProvider(
            _image_profile(), _RecordingStore()
        )
        request = urllib.request.Request("https://x", method="POST")
        with pytest.raises(urllib.error.HTTPError):
            provider._open(request)
        # 关键断言：异常透传且同一请求只发送了一次（旧行为会无条件重发）。
        assert len(calls) == 1

    def test_request_json_maps_opener_http_error_to_contract(self, monkeypatch):
        import urllib.error
        import urllib.request

        from src.shared.ai_models.generation import volcengine_generation_provider as module

        def fake_urlopen(*args, **kwargs):
            raise urllib.error.HTTPError("https://x", 429, "rate", None, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        provider = module.VolcengineSeedreamImageProvider(
            _image_profile(), _RecordingStore()
        )
        with pytest.raises(module._VolcengineProviderError) as excinfo:
            provider._request_json("POST", "https://x", {}, {})
        assert excinfo.value.code == "GEN_PROVIDER_RATE_LIMITED"


# ---------------------------------------------------------------------------
# AUD-31701：endpoint 校验 + 非契约异常兜底
# ---------------------------------------------------------------------------


class TestAud31701EndpointValidation:
    @pytest.mark.parametrize("endpoint", ["", "/api/plan/v3", "ark.example.com/api"])
    def test_invalid_endpoint_returns_config_invalid(self, endpoint):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        provider = module.VolcengineSeedreamImageProvider(
            _image_profile(endpoint=endpoint), _RecordingStore()
        )
        result = provider.generate(_request())
        assert not result.succeeded
        assert result.error_code == "GEN_PROVIDER_CONFIG_INVALID"

    def test_tts_invalid_endpoint_returns_config_invalid(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module
        from src.shared.ai_models.generation.generation_models import GenerationModality

        provider = module.VolcenginePlanTtsProvider(
            _image_profile(
                profile_id="volcengine-voice",
                provider_kind="volcengine_plan_tts",
                endpoint="",
                credential_ref="topos:voice",
                modalities=("voice",),
            ),
            _RecordingStore(),
        )
        result = provider.generate(
            _request(modality=GenerationModality.VOICE, request_id="v-1")
        )
        assert not result.succeeded
        assert result.error_code == "GEN_PROVIDER_CONFIG_INVALID"

    def test_unexpected_exception_stays_inside_contract(self, monkeypatch):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        provider = module.VolcengineSeedreamImageProvider(
            _image_profile(), _RecordingStore()
        )

        def _boom(_request):
            raise ValueError("boom outside contract")

        monkeypatch.setattr(provider, "_body", _boom)
        result = provider.generate(_request())
        assert not result.succeeded
        assert result.error_code == "GEN_PROVIDER_FAILED"


# ---------------------------------------------------------------------------
# AUD-31702：阻塞 HTTP 期间/之后的取消被尊重
# ---------------------------------------------------------------------------


class TestAud31702Cancellation:
    def test_cancelled_before_request_skips_credential_io(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        store = _RecordingStore(secret="")  # 空凭据：若先做 IO 会得到 MISSING
        provider = module.VolcengineSeedreamImageProvider(_image_profile(), store)
        result = provider.generate(_request(), is_cancelled=lambda: True)
        assert result.error_code == "GEN_PROVIDER_CANCELLED"
        assert store.get_calls == 0  # 取消优先于 keychain/文件 IO

    def test_cancel_during_blocking_request_discards_response(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        cancelled = {"flag": False}

        def opener(request, timeout):
            # 响应到达后用户才取消。
            cancelled["flag"] = True
            return _Response(json.dumps({
                "data": [{"url": "https://example.invalid/x.png"}],
            }).encode())

        provider = module.VolcengineSeedreamImageProvider(
            _image_profile(), _RecordingStore(), opener=opener
        )
        result = provider.generate(_request(), is_cancelled=lambda: cancelled["flag"])
        assert result.error_code == "GEN_PROVIDER_CANCELLED"
        assert result.candidates == ()

    def test_tts_cancel_during_request_discards_audio(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module
        from src.shared.ai_models.generation.generation_models import GenerationModality

        cancelled = {"flag": False}

        def opener(request, timeout):
            cancelled["flag"] = True
            return _Response(b"ID3-audio")

        provider = module.VolcenginePlanTtsProvider(
            _image_profile(
                profile_id="volcengine-voice",
                provider_kind="volcengine_plan_tts",
                credential_ref="topos:voice",
                modalities=("voice",),
            ),
            _RecordingStore(),
            opener=opener,
        )
        result = provider.generate(
            _request(modality=GenerationModality.VOICE, request_id="v-2"),
            is_cancelled=lambda: cancelled["flag"],
        )
        assert result.error_code == "GEN_PROVIDER_CANCELLED"
        assert result.candidates == ()


# ---------------------------------------------------------------------------
# AUD-31703：aspect_ratio 参与尺寸推导，最小像素按比例放大
# ---------------------------------------------------------------------------


class TestAud31703AspectAwareSize:
    def _provider(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        return module.VolcengineSeedreamImageProvider(_image_profile(), _RecordingStore())

    def test_no_size_info_falls_back_to_2k_square(self):
        assert self._provider()._body(_request())["size"] == "2048x2048"

    def test_request_aspect_ratio_16_9(self):
        body = self._provider()._body(_request(aspect_ratio="16:9"))
        width_s, height_s = body["size"].split("x")
        # 2048x1152 锚定后不足最小像素，等比放大到 2560x1440（保持 16:9）。
        assert (int(width_s), int(height_s)) == (2560, 1440)

    def test_request_aspect_ratio_9_16_portrait(self):
        body = self._provider()._body(_request(aspect_ratio="9:16"))
        width_s, height_s = body["size"].split("x")
        assert (int(width_s), int(height_s)) == (1440, 2560)

    def test_sub_minimum_recipe_scales_up_proportionally(self):
        from src.shared.ai_models.generation.generation_models import (
            GenerationModality,
            GenerationRecipe,
        )

        recipe = GenerationRecipe(
            recipe_id="r", modality=GenerationModality.IMAGE, width=1280, height=720,
        )
        body = self._provider()._body(_request(recipe=recipe))
        width_s, height_s = body["size"].split("x")
        width, height = int(width_s), int(height_s)
        # 不再强制 2048x2048 正方形：比率保持 16:9 且达到最小像素。
        assert width / height == pytest.approx(1280 / 720, rel=1e-3)
        assert width * height >= 3_686_400
        assert (width, height) == (2560, 1440)

    def test_large_recipe_untouched(self):
        from src.shared.ai_models.generation.generation_models import (
            GenerationModality,
            GenerationRecipe,
        )

        recipe = GenerationRecipe(
            recipe_id="r", modality=GenerationModality.IMAGE, width=4096, height=2304,
        )
        assert self._provider()._body(_request(recipe=recipe))["size"] == "4096x2304"

    def test_recipe_aspect_used_when_dimensions_absent(self):
        from src.shared.ai_models.generation.generation_models import (
            GenerationModality,
            GenerationRecipe,
        )

        recipe = GenerationRecipe(
            recipe_id="r", modality=GenerationModality.IMAGE, aspect_ratio="4:3",
        )
        body = self._provider()._body(_request(recipe=recipe))
        width_s, height_s = body["size"].split("x")
        width, height = int(width_s), int(height_s)
        # 4:3 比率保持，且达到最小像素（不再强制正方形）。
        assert width / height == pytest.approx(4 / 3, rel=1e-3)
        assert width * height >= 3_686_400


# ---------------------------------------------------------------------------
# AUD-31704：多候选 b64 不再写同一缓存文件互相覆盖
# ---------------------------------------------------------------------------


class TestAud31704DistinctCandidateCacheFiles:
    def test_two_b64_candidates_write_two_distinct_files(self, tmp_path, monkeypatch):
        import base64

        from src.shared.ai_models.generation import volcengine_generation_provider as module

        monkeypatch.setattr(module, "default_cache_dir", lambda: tmp_path)
        payloads = [b"image-AAAA", b"image-BBBB"]

        def opener(request, timeout):
            return _Response(json.dumps({
                "model": "m",
                "data": [
                    {"b64_json": base64.b64encode(p).decode()} for p in payloads
                ],
            }).encode())

        provider = module.VolcengineSeedreamImageProvider(
            _image_profile(), _RecordingStore(), opener=opener
        )
        result = provider.generate(_request(request_id="multi"))
        assert result.succeeded
        handles = [Path(c.preview_handle) for c in result.candidates]
        assert len(handles) == 2
        assert handles[0] != handles[1]
        assert handles[0].name == "multi-c1.png"
        assert handles[1].name == "multi-c2.png"
        assert handles[0].read_bytes() == payloads[0]
        assert handles[1].read_bytes() == payloads[1]


# ---------------------------------------------------------------------------
# AUD-31705：参考素材要么显式拒绝，provenance 不记录未发送的输入
# ---------------------------------------------------------------------------


class TestAud31705ReferenceAssetsHonesty:
    def test_reference_carrying_request_is_rejected_not_downgraded(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        provider = module.VolcengineSeedreamImageProvider(_image_profile(), _RecordingStore())
        result = provider.generate(
            _request(reference_asset_ids=("asset-1",)),
        )
        assert not result.succeeded
        assert result.error_code == "GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED"

    def test_provenance_records_only_what_was_sent(self):
        from src.shared.ai_models.generation import volcengine_generation_provider as module

        provider = module.VolcengineSeedreamImageProvider(_image_profile(), _RecordingStore())
        provenance = provider._provenance(
            _request(reference_asset_ids=("asset-1",)), model_version="m"
        )
        # 适配器从不上传参考素材，溯源不得记录未发送的输入。
        assert provenance.reference_asset_ids == ()
        assert provenance.reference_bindings == ()


# ---------------------------------------------------------------------------
# AUD-31800/31801：cancel 锁纪律与运行中 QThread 的引用安全
# ---------------------------------------------------------------------------


class _StubWorker:
    """AIInferenceEngine 只用到 cancel/wait/isFinished/cache_key。"""

    def __init__(self, cache_key: str, wait_result: bool = True) -> None:
        self.cache_key = cache_key
        self.cancelled = False
        self._wait_result = wait_result
        self._finished = wait_result

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, _ms: int) -> bool:
        time.sleep(0.2)
        return self._wait_result

    def isFinished(self) -> bool:
        return self._finished


class TestAud31800CancelDoesNotHoldLock:
    def test_engine_stays_usable_while_cancel_waits(self):
        from src.shared.ai_models.inference_engine import AIInferenceEngine

        engine = AIInferenceEngine()
        engine._workers["slow"] = _StubWorker("slow", wait_result=True)
        thread = threading.Thread(target=engine.cancel, args=("slow",))
        thread.start()
        time.sleep(0.05)  # 让 cancel 进入 wait()
        start = time.perf_counter()
        assert engine.is_processing("other") is False  # 需要同一把锁
        elapsed = time.perf_counter() - start
        thread.join(timeout=5)
        # 旧行为：wait(3000) 持锁，is_processing 被阻塞至超时。
        assert elapsed < 0.15, f"cancel held the engine lock for {elapsed:.3f}s"

    def test_cancel_all_waits_outside_the_lock(self):
        from src.shared.ai_models.inference_engine import AIInferenceEngine

        engine = AIInferenceEngine()
        engine._workers["a"] = _StubWorker("a")
        engine._workers["b"] = _StubWorker("b")
        thread = threading.Thread(target=engine.cancel_all)
        thread.start()
        time.sleep(0.05)
        start = time.perf_counter()
        engine.is_processing("other")
        elapsed = time.perf_counter() - start
        thread.join(timeout=10)
        assert elapsed < 0.15


class TestAud31801RetiredWorkerReference:
    def test_timeout_keeps_worker_referenced_until_finished(self):
        from src.shared.ai_models.inference_engine import AIInferenceEngine

        engine = AIInferenceEngine()
        stub = _StubWorker("stuck", wait_result=False)  # wait 超时
        engine._workers["stuck"] = stub
        engine.cancel("stuck")
        # 超时：强引用必须保留，避免运行中的 QThread 被 GC 销毁。
        assert stub in engine._retired_workers
        assert engine._workers == {}

        # 线程真正退出后，随引擎活动被清理。
        stub._finished = True
        engine.is_processing("anything")
        assert engine._retired_workers == []

    def test_finished_worker_not_retired(self):
        from src.shared.ai_models.inference_engine import AIInferenceEngine

        engine = AIInferenceEngine()
        stub = _StubWorker("done", wait_result=True)
        engine._workers["done"] = stub
        engine.cancel("done")
        assert engine._retired_workers == []
        assert stub.cancelled is True


# ---------------------------------------------------------------------------
# AUD-31802：tile/overlap 参数关系校验
# ---------------------------------------------------------------------------


class _FakeOnnxSession:
    """透传推理：输出 == 输入（NCHW）。"""

    def get_inputs(self):
        return [SimpleNamespace(name="input")]

    def get_outputs(self):
        return [SimpleNamespace(name="output")]

    def run(self, _names, feed):
        return [feed["input"]]


class TestAud31802TilingValidation:
    def test_normalize_clamps_tile_size_and_overlap(self):
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        # tile_size 下限。
        assert AIInferenceWorker._normalize_tiling(64, 64) == (128, 64)
        # overlap 上限 tile//2：stride 恒 > 0。
        assert AIInferenceWorker._normalize_tiling(512, 600) == (512, 256)
        assert AIInferenceWorker._normalize_tiling(512, -5) == (512, 0)
        # 合法组合不动。
        assert AIInferenceWorker._normalize_tiling(512, 64) == (512, 64)

    def test_overlap_one_ramp_no_division_by_zero(self):
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        worker = AIInferenceWorker(
            "k", "ai_denoise", {"image": np.zeros((4, 4, 3), np.float32)}, {}
        )
        image = np.random.rand(16, 16, 3).astype(np.float32)
        result = worker._process_tiled(
            image, None, _FakeOnnxSession(), tile_size=128, overlap=1
        )
        assert np.isfinite(result).all()
        assert result.shape == image.shape

    def test_empty_tile_plan_raises_instead_of_black_image(self):
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        worker = AIInferenceWorker(
            "k", "ai_denoise", {"image": np.zeros((4, 4, 3), np.float32)}, {}
        )
        empty = np.zeros((0, 4, 3), dtype=np.float32)
        with pytest.raises(ValueError):
            worker._process_tiled(empty, None, _FakeOnnxSession(), 128, 0)


# ---------------------------------------------------------------------------
# AUD-31803：用户取消不再走 error 信号
# ---------------------------------------------------------------------------


class TestAud31803CancelledNotReportedAsError:
    def _make_worker(self, qapp):
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        worker = AIInferenceWorker(
            "k",
            "ai_denoise",
            {"image": np.zeros((8, 8, 3), np.float32)},
            {"use_tiling": False, "enable_preview": False},
        )
        worker._get_model_path = lambda: "/fake/model.onnx"  # type: ignore[assignment]
        worker._load_onnx_session = lambda _p: _FakeOnnxSession()  # type: ignore[assignment]
        worker._emit_preview = lambda *a, **k: None  # type: ignore[assignment]
        return worker

    def test_cancel_exception_is_silent(self, qapp):
        from src.shared.ai_models.inference_engine import _InferenceCancelledError

        worker = self._make_worker(qapp)
        events: list[str] = []
        worker.error.connect(lambda *a: events.append("error"))
        worker.finished.connect(lambda *a: events.append("finished"))

        def _cancelled_run(*args, **kwargs):
            raise _InferenceCancelledError()

        worker._process_full_resolution = _cancelled_run  # type: ignore[assignment]
        worker.run()
        assert events == []  # 取消既非失败也非完成

    def test_real_failure_still_reports_error(self, qapp):
        worker = self._make_worker(qapp)
        events: list[str] = []
        worker.error.connect(lambda *a: events.append("error"))

        def _failing_run(*args, **kwargs):
            raise RuntimeError("real failure")

        worker._process_full_resolution = _failing_run  # type: ignore[assignment]
        worker.run()
        assert events == ["error"]


# ---------------------------------------------------------------------------
# AUD-31804：HDR 输入显式范围映射，输出对称恢复
# ---------------------------------------------------------------------------


class TestAud31804HdrRangeMapping:
    def test_hdr_scale_for(self):
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        hdr = np.array([[[0.0, 2.0, 4.0]]], dtype=np.float32)
        sdr = np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32)
        assert AIInferenceWorker._hdr_scale_for(hdr) == 4.0
        assert AIInferenceWorker._hdr_scale_for(sdr) == 1.0
        # 整数 dtype 不在本引擎契约内，不做缩放。
        assert AIInferenceWorker._hdr_scale_for(np.array([[[255, 0, 0]]], np.uint8)) == 1.0

    def test_full_pipeline_preserves_hdr_highlights(self, qapp):
        """透传模型下，HDR 输入（峰值 4.0）的输出峰值应恢复到 4.0，
        而不是被输出侧 clip(0,1) 截断。"""
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        image = np.zeros((8, 8, 3), dtype=np.float32)
        image[..., 0] = np.linspace(0.0, 4.0, 64).reshape(8, 8)  # HDR 高光

        worker = AIInferenceWorker(
            "k", "ai_denoise", {"image": image},
            {"use_tiling": False, "enable_preview": False},
        )
        worker._get_model_path = lambda: "/fake/model.onnx"  # type: ignore[assignment]
        worker._load_onnx_session = lambda _p: _FakeOnnxSession()  # type: ignore[assignment]
        worker._emit_preview = lambda *a, **k: None  # type: ignore[assignment]

        results: list[np.ndarray] = []
        worker.finished.connect(lambda _k, arr, _rid: results.append(arr))
        worker.run()

        assert results, "inference should finish"
        result = results[0]
        assert float(result.max()) == pytest.approx(4.0, rel=1e-4)
        assert np.allclose(result, image, atol=1e-4)

    def test_blend_uses_same_domain_as_result(self, qapp):
        """blend=0.5 + 透传模型：输出 = 原图（两域一致才成立）。"""
        from src.shared.ai_models.inference_engine import AIInferenceWorker

        image = np.full((8, 8, 3), 2.0, dtype=np.float32)  # HDR
        worker = AIInferenceWorker(
            "k", "ai_denoise", {"image": image},
            {"use_tiling": False, "enable_preview": False, "blend": 0.5},
        )
        worker._get_model_path = lambda: "/fake/model.onnx"  # type: ignore[assignment]
        worker._load_onnx_session = lambda _p: _FakeOnnxSession()  # type: ignore[assignment]
        worker._emit_preview = lambda *a, **k: None  # type: ignore[assignment]

        results: list[np.ndarray] = []
        worker.finished.connect(lambda _k, arr, _rid: results.append(arr))
        worker.run()

        result = results[0]
        # 透传模型 + blend 0.5：0.5*norm + 0.5*norm = norm，恢复后 = 原图。
        assert np.allclose(result, image, atol=1e-4)


# ---------------------------------------------------------------------------
# AUD-31809：模型下载原子落位与残留清理
# ---------------------------------------------------------------------------


class _FakeDownloadResponse:
    def __init__(self, chunks, total=None):
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(total if total else sum(len(c) for c in chunks))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.fixture()
def registry_guard():
    """MODEL_REGISTRY 是模块级可变全局：快照/恢复每条 ModelInfo 状态。"""
    from src.shared.ai_models import model_manager as mm

    snapshot = {
        mid: (info.local_path, info.is_available, info.file_size, info.download_progress)
        for mid, info in mm.MODEL_REGISTRY.items()
    }
    yield mm
    for mid, (local_path, is_available, file_size, progress) in snapshot.items():
        info = mm.MODEL_REGISTRY[mid]
        info.local_path = local_path
        info.is_available = is_available
        info.file_size = file_size
        info.download_progress = progress


class TestAud31809AtomicDownload:
    def _worker(self, mm, target, model_id="sam_decoder"):
        return mm.ModelDownloadWorker(
            model_id, mm.MODEL_REGISTRY[model_id], str(target)
        )

    def test_success_writes_target_atomically_no_part_left(
        self, registry_guard, monkeypatch, tmp_path
    ):
        import urllib.request

        mm = registry_guard
        target = tmp_path / "mask" / "sam_decoder.onnx"
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout: _FakeDownloadResponse([b"ABCD", b"EFGH"]),
        )
        outcomes: list[tuple] = []
        worker = self._worker(mm, target)
        worker.finished.connect(lambda *a: outcomes.append(a))
        worker.run()

        assert outcomes == [("sam_decoder", True, "")]
        assert target.read_bytes() == b"ABCDEFGH"
        assert not Path(str(target) + ".part").exists()

    def test_network_failure_cleans_partial_and_target_absent(
        self, registry_guard, monkeypatch, tmp_path
    ):
        import urllib.error
        import urllib.request

        mm = registry_guard
        target = tmp_path / "mask" / "sam_decoder.onnx"

        def failing_urlopen(req, timeout):
            return _FakeDownloadResponse([b"PART"], total=100)

        # 让写入中途抛 URLError：读完第一块后失败。
        class _BrokenResponse(_FakeDownloadResponse):
            def read(self, n):
                if not self._chunks:
                    raise urllib.error.URLError("connection reset")
                return self._chunks.pop(0)

        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout: _BrokenResponse([b"PART"], total=100),
        )
        outcomes: list[tuple] = []
        worker = self._worker(mm, target)
        worker.finished.connect(lambda *a: outcomes.append(a))
        worker.run()

        assert outcomes and outcomes[0][1] is False
        assert not target.exists()  # 半截文件不得出现在最终路径
        assert not Path(str(target) + ".part").exists()

    def test_cancel_cleans_partial(self, registry_guard, monkeypatch, tmp_path):
        import urllib.request

        mm = registry_guard
        target = tmp_path / "mask" / "sam_decoder.onnx"
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout: _FakeDownloadResponse([b"PARTIAL"], total=1000),
        )
        outcomes: list[tuple] = []
        worker = self._worker(mm, target)
        worker.finished.connect(lambda *a: outcomes.append(a))
        worker.cancel()  # 下载中取消（run 前置位等价于首块后取消）
        worker.run()
        assert outcomes and outcomes[0][1] is False
        assert not target.exists()
        assert not Path(str(target) + ".part").exists()

    def test_sync_registry_ignores_zero_byte_leftover(
        self, registry_guard, tmp_path
    ):
        mm = registry_guard
        cache = tmp_path / "models"
        zero = cache / "denoise" / "nafnet.onnx"
        zero.parent.mkdir(parents=True, exist_ok=True)
        zero.write_bytes(b"")  # 旧版本异常中断留下的零字节残留

        manager = mm.AIModelManager(cache_dir=str(cache))
        assert manager.is_model_available("nafnet_denoise") is False


# ---------------------------------------------------------------------------
# AUD-31816：PatchMerging.forward 前向可用
# ---------------------------------------------------------------------------


class TestAud31816PatchMergingForward:
    def test_forward_reduces_resolution_without_name_error(self):
        # torch 为 SwinIR 可选重依赖：未安装环境跳过而非误报失败
        #（与 ai 模型类测试的 importorskip 口径一致）。
        pytest.importorskip("torch", reason="SwinIR weights optional")

        from src.shared.ai_models.swinir import PatchMerging

        layer = PatchMerging(input_resolution=(8, 8), dim=4)
        x = torch.randn(1, 64, 4)  # B, L=8*8, C=4
        out = layer(x)
        assert out.shape == (1, 16, 8)  # 4*C -> 2*C, 空间减半
        assert torch.isfinite(out).all()

    def test_source_no_longer_references_bare_dim(self):
        source = Path(
            "src/shared/ai_models/swinir.py"
        ).read_text(encoding="utf-8")
        assert "4 * dim)" not in source
        assert "4 * C)" in source


# ---------------------------------------------------------------------------
# AUD-32001/32002/32003：图标正确性
# ---------------------------------------------------------------------------


class TestAud32001FavoriteOutline:
    def test_outline_and_solid_use_different_glyphs(self):
        from src.shared.assets.icon_manager import ICON_MAP

        assert ICON_MAP["favorite"] == "fa5s.star"
        assert ICON_MAP["favorite_outline"] == "fa5.star"

    def test_outline_glyph_renders(self, qapp):
        import qtawesome as qta

        icon = qta.icon("fa5.star", color="#ffffff")  # charmap 已含 star
        assert icon is not None


class TestAud32002IntersectGlyph:
    def test_intersect_glyph_exists_in_charmap(self, qapp):
        import qtawesome as qta

        from src.shared.assets.icon_manager import ICON_MAP

        # 'object-intersect' 不存在于 FA5 free charmap，会抛异常。
        icon = qta.icon(ICON_MAP["op_intersect"], color="#ffffff")
        assert icon is not None

    def test_every_icon_map_glyph_is_renderable(self, qapp):
        """审计建议的全量字形存在性校验（防同类问题再入库）。"""
        import qtawesome as qta

        from src.shared.assets.icon_manager import ICON_MAP

        for key, name in ICON_MAP.items():
            try:
                qta.icon(name, color="#ffffff")
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"ICON_MAP['{key}'] uses missing glyph {name!r}: {exc}")


class TestAud32003MaskIconTinting:
    def test_svg_path_applies_requested_color(self, qapp):
        from PyQt6.QtGui import QColor

        from src.shared.assets.icon_manager import IconColors, icon_mask

        icon = icon_mask(IconColors.ORANGE)
        image = icon.pixmap(64, 64).toImage()
        expected = QColor(IconColors.ORANGE).rgb() & 0xFFFFFF
        expected_color = QColor(IconColors.ORANGE)
        found_opaque = 0
        found_tinted = 0
        for y in range(0, 64, 2):
            for x in range(0, 64, 2):
                color = image.pixelColor(x, y)
                if color.alpha() == 0:
                    continue
                found_opaque += 1
                exact = (color.rgb() & 0xFFFFFF) == expected
                # 部分透明的抗锯齿边缘在 8bit 预乘/反预乘下有舍入，且
                # 量化误差随 alpha 变小而放大（≈256/alpha）。
                tolerance = max(8, 256 // color.alpha())
                close = all(
                    abs(a - b) <= tolerance
                    for a, b in (
                        (color.red(), expected_color.red()),
                        (color.green(), expected_color.green()),
                        (color.blue(), expected_color.blue()),
                    )
                )
                if exact or close:
                    found_tinted += 1
        assert found_opaque > 0, "mask glyph should have visible pixels"
        # 着色后所有可见像素都是请求颜色（旧实现按 SVG 原色渲染）。
        assert found_tinted == found_opaque

    def test_inactive_and_active_differ(self, qapp):
        from src.shared.assets.icon_manager import IconColors, icon_mask

        def _sample(color: str) -> int:
            image = icon_mask(color).pixmap(64, 64).toImage()
            samples = [
                image.pixelColor(x, y).rgb() & 0xFFFFFF
                for y in range(0, 64, 4)
                for x in range(0, 64, 4)
                if image.pixelColor(x, y).alpha() > 0
            ]
            assert samples, "glyph should have visible pixels"
            return samples[0]

        assert _sample(IconColors.DEFAULT) != _sample(IconColors.ORANGE)
