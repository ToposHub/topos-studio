"""WD17-W4 / AUD-6311：AI Gen Expand 生产 generation transport 接线。

缺陷：``set_generation_transport`` 在生产代码中零调用——节点注册进
NODE_REGISTRY 可被用户创建，但没有任何 page/适配器注入 transport，生产
构建里恒 NEEDS_CONFIG 直通（功能与实现脱节）。

修复：

* ``ai_gen_expand_node.install_default_generation_transport()`` 从共享
  generation 栈（用户级 ``~/.topos/ai_profiles.json``）解析第一个支持
  image 模态的 profile，把 Volcengine Seedream provider 适配成节点
  transport 契约 ``(request, api_key, output_size) -> RGBA``；
* ``CompositeModule.__init__`` 经 ``_wire_ai_generation_transport()``
  完成进程级安装（无 profile 时保持可见 NEEDS_CONFIG 直通，不致命）。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.composite.nodes import ai_gen_expand_node as node_mod
from src.features.composite.nodes.ai_gen_expand_node import (
    AIGenExpandNode,
    install_default_generation_transport,
    reset_generation_transport,
    set_generation_transport,
)

pytestmark = pytest.mark.composite_core


@pytest.fixture(autouse=True)
def _reset_transport():
    """每个用例前后清空进程级 transport，避免污染其他测试。"""
    reset_generation_transport()
    yield
    reset_generation_transport()


# ── 夹具 ──────────────────────────────────────────────────────────────

_IMAGE_PROFILE = {
    "profile_id": "volcengine-image",
    "display_name": "Volcengine Image",
    "provider_kind": "volcengine",
    "endpoint": "https://ark.example.com/api/v3",
    "model_id": "seedream-5.0-lite",
    "credential_ref": "topos:volcengine-image",
    "modalities": ["text", "image"],
}

_TEXT_ONLY_PROFILE = {
    "profile_id": "local-llm",
    "display_name": "Local LLM",
    "modalities": ["text", "tool_call"],
}


class _FakeStore:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)
        self.requested_refs: list[str] = []

    def get_secret(self, credential_ref: str):
        self.requested_refs.append(credential_ref)
        return self._secrets.get(credential_ref)


class _FakeProvider:
    """transport 依赖的最小 provider 表面：generate(request) -> result。"""

    def __init__(self, result, record=None) -> None:
        self._result = result
        self._record = record if record is not None else []

    def generate(self, request):
        self._record.append(request)
        return self._result


def _success_result(handle: str):
    return SimpleNamespace(
        succeeded=True,
        candidates=[SimpleNamespace(preview_handle=handle)],
        error_code="",
        error_message="",
    )


def _error_result(code: str = "GEN_PROVIDER_FAILED", message: str = "boom"):
    return SimpleNamespace(
        succeeded=False,
        candidates=(),
        error_code=code,
        error_message=message,
    )


def _write_png(path, size=(7, 5), color=(255, 0, 0, 255)) -> str:
    from PIL import Image

    Image.new("RGBA", size, color).save(path, format="PNG")
    return str(path)


# ── 安装决策 ──────────────────────────────────────────────────────────


class TestInstallDecision:

    def test_no_profiles_config_not_wired(self) -> None:
        assert install_default_generation_transport(config={}) is False
        assert node_mod._TRANSPORT is None

    def test_profiles_not_a_list_not_wired(self) -> None:
        assert install_default_generation_transport(
            config={"profiles": "broken"}) is False
        assert node_mod._TRANSPORT is None

    def test_corrupt_profile_entries_skipped(self) -> None:
        # 单条损坏（缺 profile_id）不拖垮整批：跳过后仍能找到合法 image 条目
        config = {"profiles": [{"display_name": "no id"}, _IMAGE_PROFILE]}
        assert install_default_generation_transport(
            config=config, credential_store=_FakeStore({})) is True

    def test_no_image_capable_profile_not_wired(self) -> None:
        config = {"profiles": [_TEXT_ONLY_PROFILE]}
        assert install_default_generation_transport(
            config=config, credential_store=_FakeStore({})) is False
        assert node_mod._TRANSPORT is None

    def test_image_profile_installs_transport_and_key_provider(self) -> None:
        store = _FakeStore({"topos:volcengine-image": "sk-live"})
        ok = install_default_generation_transport(
            config={"profiles": [_TEXT_ONLY_PROFILE, _IMAGE_PROFILE]},
            credential_store=store,
            provider_factory=lambda profile, s: _FakeProvider(_error_result()),
        )
        assert ok is True
        assert node_mod._TRANSPORT is not None
        assert node_mod._KEY_PROVIDER is not None
        assert node_mod._KEY_PROVIDER() == "sk-live"
        assert store.requested_refs == ["topos:volcengine-image"]

    def test_missing_key_provider_returns_empty_string(self) -> None:
        # 无密钥 → key provider 返回 ""：job.run 呈现 NEEDS_CONFIG（与
        # 未配置 key 的既有可见语义一致），不再是无 transport 的永久直通。
        install_default_generation_transport(
            config={"profiles": [_IMAGE_PROFILE]},
            credential_store=_FakeStore({}),
            provider_factory=lambda profile, s: _FakeProvider(_error_result()),
        )
        assert node_mod._KEY_PROVIDER() == ""


# ── transport 契约 ────────────────────────────────────────────────────


class TestTransportContract:

    def test_transport_decodes_candidate_to_rgba(self, tmp_path) -> None:
        handle = _write_png(tmp_path / "candidate.png")
        requests = []
        provider = _FakeProvider(_success_result(handle), record=requests)
        install_default_generation_transport(
            config={"profiles": [_IMAGE_PROFILE]},
            credential_store=_FakeStore({"topos:volcengine-image": "sk"}),
            provider_factory=lambda profile, s: provider,
        )
        transport = node_mod._TRANSPORT
        assert transport is not None

        rgba = transport(
            {"prompt": "extend the sky", "negative_prompt": "blurry",
             "seed": 3, "strength": 0.8},
            "sk", (16, 9),
        )
        assert isinstance(rgba, np.ndarray)
        assert rgba.dtype == np.float32
        assert rgba.shape == (9, 16, 4)
        assert rgba.min() >= 0.0 and rgba.max() <= 1.0
        # 纯红 PNG → R=1，且被重采样到请求尺寸
        assert rgba[..., 0] == pytest.approx(1.0, abs=1e-6)
        assert rgba[..., 1] == pytest.approx(0.0, abs=1e-6)

        # 请求字段映射到 provider-neutral 请求
        assert len(requests) == 1
        gen_request = requests[0]
        assert gen_request.prompt == "extend the sky"
        assert gen_request.negative_prompt == "blurry"
        assert gen_request.recipe is not None
        assert (gen_request.recipe.width, gen_request.recipe.height) == (16, 9)
        assert gen_request.recipe.seed == 3
        assert gen_request.consent.confirmed is True

    def test_transport_raises_on_provider_error(self, tmp_path) -> None:
        install_default_generation_transport(
            config={"profiles": [_IMAGE_PROFILE]},
            credential_store=_FakeStore({}),
            provider_factory=lambda profile, s: _FakeProvider(_error_result()),
        )
        transport = node_mod._TRANSPORT
        with pytest.raises(RuntimeError, match="GEN_PROVIDER_FAILED"):
            transport({"prompt": "x"}, "sk", (4, 4))

    def test_transport_rejects_empty_prompt(self) -> None:
        install_default_generation_transport(
            config={"profiles": [_IMAGE_PROFILE]},
            credential_store=_FakeStore({}),
            provider_factory=lambda profile, s: _FakeProvider(
                _success_result("unused")),
        )
        with pytest.raises(ValueError, match="prompt"):
            node_mod._TRANSPORT({"prompt": ""}, "sk", (4, 4))


# ── 节点行为 ──────────────────────────────────────────────────────────


class TestNodeStateWithWiring:

    def test_node_leaves_needs_config_only_without_wiring(self) -> None:
        # 未接线（本测试的 autouse fixture 已 reset）：节点可见 NEEDS_CONFIG 直通
        node = AIGenExpandNode()
        assert node_mod._TRANSPORT is None
        # _do_evaluate 的 NEEDS_CONFIG 早退由既有套件（test_ai_gen_expand）
        # 锁定；这里锁定安装后该早退条件不再成立。
        install_default_generation_transport(
            config={"profiles": [_IMAGE_PROFILE]},
            credential_store=_FakeStore({}),
            provider_factory=lambda profile, s: _FakeProvider(_error_result()),
        )
        assert node_mod._TRANSPORT is not None


# ── CompositeModule 接线 ──────────────────────────────────────────────


class TestCompositeModuleWiring:

    def test_init_calls_wire_hook(self) -> None:
        from src.features.composite.module import CompositeModule

        source = inspect.getsource(CompositeModule.__init__)
        assert "_wire_ai_generation_transport()" in source, (
            "CompositeModule.__init__ 必须完成 transport 接线（AUD-6311）")

    def test_wire_hook_invokes_install(self, monkeypatch) -> None:
        from src.features.composite.module import CompositeModule

        called: dict[str, bool] = {}

        def _fake_install() -> bool:
            called["installed"] = True
            return True

        monkeypatch.setattr(
            node_mod, "install_default_generation_transport", _fake_install)
        CompositeModule._wire_ai_generation_transport()
        assert called.get("installed") is True

    def test_wire_hook_degrades_gracefully(self, monkeypatch) -> None:
        from src.features.composite.module import CompositeModule

        def _exploding_install() -> bool:
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(
            node_mod, "install_default_generation_transport", _exploding_install)
        # 接线失败绝不致命：吞掉并记 warning
        CompositeModule._wire_ai_generation_transport()
        assert node_mod._TRANSPORT is None
