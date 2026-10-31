"""TAEP M4 — ModelPackManager 测试。

验收（§16.4 / §11.1 M4-1）：
- 内置 spec（WhisperX + pyannote）许可证/体积/硬件/sha256 完整。
- 安装状态探测 + sha256 完整性校验（损坏标记 verified=False，不静默）。
- require_usable 模型缺失抛 ModelPackNotInstalledError（不静默降级）。
- recipe → 所需 pack 解析（EDITORIAL_LOCAL = transcript + align + diarization）。
- mark_installed / remove / manifest 持久化。
- 商用许可证区分（pyannote CC-BY-NC → commercial_use=False，UI 据此警告）。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.model_pack_manager import (
    BUILTIN_SPECS,
    DEFAULT_CACHE_DIR,
    ModelPackLicense,
    ModelPackManager,
    ModelPackNotInstalledError,
    ModelPackSpec,
)


# ---------------------------------------------------------------------- #
# 内置 spec 完整性
# ---------------------------------------------------------------------- #


class TestBuiltinSpecs:
    def test_builtin_specs_cover_m4_capabilities(self) -> None:
        capabilities = {s.capability for s in BUILTIN_SPECS}
        assert "transcript" in capabilities
        assert "align" in capabilities
        assert "diarization" in capabilities

    def test_every_spec_has_license_and_size(self) -> None:
        for spec in BUILTIN_SPECS:
            assert isinstance(spec.license, ModelPackLicense)
            assert spec.license.name  # 非空
            assert spec.size_bytes > 0
            assert spec.sha256  # 完整性校验字段存在

    def test_pyannote_is_non_commercial(self) -> None:
        """R4-c：pyannote pack 已移除（被 Sortformer 替代）；此测试验证不再有非商用 pack。

        旧 WhisperX/pyannote 已彻底删除——所有保留的 pack 都应该商用 OK（Apache-2.0）。
        """
        for spec in BUILTIN_SPECS:
            # 所有保留 pack 都应商用友好（无 CC-BY-NC）
            assert spec.license.commercial_use is True, (
                f"pack {spec.pack_id!r} has commercial_use=False; "
                "R4-c removed non-commercial packs (pyannote)"
            )

    def test_whisperx_is_commercial_ok(self) -> None:
        """R4-c：whisperx pack 已移除；保留的 ASR pack（qwen3_asr）商用 OK。"""
        asr = next(s for s in BUILTIN_SPECS if s.pack_id == "qwen3_asr")
        assert asr.license.commercial_use is True

    def test_qwen3_and_sortformer_are_commercial_ok(self) -> None:
        """R4-c：2026 现代替代（Qwen3-ASR / Sortformer）全部 Apache-2.0 商用 OK。"""
        qwen3_asr = next(s for s in BUILTIN_SPECS if s.pack_id == "qwen3_asr")
        qwen3_align = next(s for s in BUILTIN_SPECS if s.pack_id == "qwen3_align")
        sortformer = next(s for s in BUILTIN_SPECS if s.pack_id == "nemo_sortformer")
        for spec in (qwen3_asr, qwen3_align, sortformer):
            assert spec.license.commercial_use is True
            assert "Apache" in spec.license.name

    def test_gpu_specs_declare_vram(self) -> None:
        for spec in BUILTIN_SPECS:
            if spec.requires_gpu:
                assert spec.min_vram_mb > 0

    def test_to_dict_roundtrip(self) -> None:
        spec = BUILTIN_SPECS[0]
        d = spec.to_dict()
        assert d["pack_id"] == spec.pack_id
        assert d["license"]["commercial_use"] == spec.license.commercial_use
        assert "languages" in d


# ---------------------------------------------------------------------- #
# Manager 注册 + 状态探测
# ---------------------------------------------------------------------- #


class TestManagerRegistration:
    def test_default_manager_loads_builtin_specs(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path)
        ids = {s.pack_id for s in mgr.list_specs()}
        assert "qwen3_asr" in ids
        assert "nemo_sortformer" in ids

    def test_list_specs_is_sorted(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path)
        ids = [s.pack_id for s in mgr.list_specs()]
        assert ids == sorted(ids)

    def test_register_custom_spec(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path, specs=())
        custom = ModelPackSpec(
            pack_id="custom_model",
            capability="transcript",
            version="1.0",
            size_bytes=1000,
            sha256="sha256:custom-placeholder",
            languages=("en",),
            requires_gpu=False,
            min_vram_mb=0,
            license=ModelPackLicense(
                name="custom", commercial_use=True, redistribution=True,
                attribution_required=False,
            ),
        )
        mgr.register_spec(custom)
        assert mgr.get_spec("custom_model") is not None
        assert "custom_model" in [s.pack_id for s in mgr.list_specs()]

    def test_get_status_unknown_pack(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path)
        status = mgr.get_status("does_not_exist")
        assert status.installed is False
        assert status.verified is False


# ---------------------------------------------------------------------- #
# 安装 + sha256 校验
# ---------------------------------------------------------------------- #


class TestInstallAndVerify:
    def test_uninstalled_pack_raises_require_usable(self, tmp_path) -> None:
        """§11.1 M4：模型缺失抛错，不静默降级。"""
        mgr = ModelPackManager(cache_dir=tmp_path)
        with pytest.raises(ModelPackNotInstalledError) as exc_info:
            mgr.require_usable("qwen3_asr")
        assert exc_info.value.pack_id == "qwen3_asr"

    def test_default_manager_rejects_placeholder_sha(self, tmp_path) -> None:
        """生产默认严格模式不能把占位 SHA 当成真实模型。"""
        mgr = ModelPackManager(cache_dir=tmp_path)
        spec = mgr.get_spec("qwen3_asr")
        assert spec is not None
        install_dir = tmp_path / "qwen3_asr"
        install_dir.mkdir()
        ok = mgr.mark_installed("qwen3_asr", install_dir)
        assert ok is False
        status = mgr.get_status("qwen3_asr")
        assert status.installed is True
        assert status.verified is False
        assert not status.usable

    def test_explicit_demo_manager_accepts_placeholder_sha(self, tmp_path) -> None:
        """确定性测试桩必须显式选择 demo verification。"""
        mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        install_dir = tmp_path / "qwen3_asr"
        install_dir.mkdir()
        assert mgr.mark_installed("qwen3_asr", install_dir) is True
        assert mgr.get_status("qwen3_asr").usable

    def test_require_usable_after_install(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        install_dir = tmp_path / "qwen3_asr"
        install_dir.mkdir()
        mgr.mark_installed("qwen3_asr", install_dir)
        path = mgr.require_usable("qwen3_asr")
        assert os.path.isdir(path)

    def test_missing_dir_marks_not_installed(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        install_dir = tmp_path / "ghost"
        install_dir.mkdir()
        mgr.mark_installed("qwen3_asr", install_dir)
        # 删目录后状态变 not installed
        import shutil
        shutil.rmtree(install_dir)
        status = mgr.get_status("qwen3_asr")
        assert status.installed is False

    def test_spec_sha_upgrade_invalidates_old_cache(self, tmp_path) -> None:
        """spec 升级（sha256 变化）后旧 cache 视为未校验（防误用旧模型）。"""
        spec_v1 = ModelPackSpec(
            pack_id="test_model", capability="transcript", version="1",
            size_bytes=1, sha256="sha256:builtin-test_model-v1-placeholder", languages=("en",),
            requires_gpu=False, min_vram_mb=0,
            license=ModelPackLicense("MIT", True, True, True),
        )
        mgr = ModelPackManager(cache_dir=tmp_path, specs=(spec_v1,), strict_verification=False)
        install_dir = tmp_path / "test_model"
        install_dir.mkdir()
        mgr.mark_installed("test_model", install_dir)
        assert mgr.get_status("test_model").verified is True
        # 升级 spec（sha 变化）
        spec_v2 = ModelPackSpec(
            pack_id="test_model", capability="transcript", version="2",
            size_bytes=1, sha256="sha256:builtin-test_model-v2-placeholder", languages=("en",),
            requires_gpu=False, min_vram_mb=0,
            license=ModelPackLicense("MIT", True, True, True),
        )
        mgr.register_spec(spec_v2)
        # 旧 cache 的 sha 与新 spec 不符 → verified=False
        status = mgr.get_status("test_model")
        assert status.installed is True
        assert status.verified is False
        assert status.usable is False


# ---------------------------------------------------------------------- #
# remove + manifest 持久化
# ---------------------------------------------------------------------- #


class TestRemoveAndManifest:
    def test_remove_deletes_record_and_files(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        install_dir = tmp_path / "qwen3_asr"
        install_dir.mkdir()
        (install_dir / "model.bin").write_bytes(b"x")
        mgr.mark_installed("qwen3_asr", install_dir)
        assert mgr.get_status("qwen3_asr").installed

        removed = mgr.remove("qwen3_asr", delete_files=True)
        assert removed is True
        assert not mgr.get_status("qwen3_asr").installed
        assert not install_dir.exists()  # 文件被删

    def test_remove_unknown_returns_false(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path)
        assert mgr.remove("not_installed") is False

    def test_manifest_persisted_across_instances(self, tmp_path) -> None:
        mgr1 = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        install_dir = tmp_path / "qwen3_asr"
        install_dir.mkdir()
        mgr1.mark_installed("qwen3_asr", install_dir)
        # 新实例从 manifest 读回
        mgr2 = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        assert mgr2.get_status("qwen3_asr").installed is True

    def test_remove_outside_cache_keeps_files(self, tmp_path) -> None:
        """install_path 在 cache_dir 之外时不删文件（§0 安全）。"""
        mgr = ModelPackManager(cache_dir=tmp_path / "cache", strict_verification=False)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "model.bin").write_bytes(b"x")
        mgr.mark_installed("qwen3_asr", outside, verify=False)
        mgr.remove("qwen3_asr", delete_files=True)
        # 记录删了但文件保留（cache 外不删）
        assert outside.exists()


# ---------------------------------------------------------------------- #
# recipe 解析
# ---------------------------------------------------------------------- #


class TestRecipeResolution:
    def test_fast_local_needs_no_packs(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path)
        packs = mgr.resolve_for_recipe("fast_local")
        assert packs == ()

    def test_editorial_local_needs_transcript_align_diarize(self, tmp_path) -> None:
        mgr = ModelPackManager(cache_dir=tmp_path)
        packs = mgr.resolve_for_recipe("editorial_local", available_languages=("en",))
        capabilities = {p.capability for p in packs}
        assert capabilities == {"transcript", "align", "diarization"}

    def test_language_filter_skips_non_covering_align(self, tmp_path) -> None:
        """align pack 语言覆盖过滤逻辑。

        R4-c：旧 WhisperX/pyannote pack 已删除。用 inline 构造一个仅英文的 align pack
        验证语言过滤逻辑仍然生效（中文素材不应匹配仅英文的 align）。
        """
        from src.features.edit.ai_editing.model_pack_manager import (
            ModelPackLicense, ModelPackSpec,
        )

        en_only_align = ModelPackSpec(
            pack_id="test_align_en_only", capability="align", version="test",
            size_bytes=1, sha256="sha256:test-placeholder",
            languages=("en",),  # 仅英文
            requires_gpu=False, min_vram_mb=0,
            license=ModelPackLicense("test", True, True, True),
        )
        any_lang_asr = ModelPackSpec(
            pack_id="test_asr", capability="transcript", version="test",
            size_bytes=1, sha256="sha256:test-placeholder",
            languages=(),  # 语言无关
            requires_gpu=False, min_vram_mb=0,
            license=ModelPackLicense("test", True, True, True),
        )
        mgr = ModelPackManager(
            cache_dir=tmp_path, specs=(en_only_align, any_lang_asr), strict_verification=False,
        )
        # 中文素材：transcript 语言无关 → 返回；align 仅英文 → 被过滤
        packs_zh = mgr.resolve_for_recipe("editorial_local", available_languages=("zh",))
        capabilities = {p.capability for p in packs_zh}
        assert "transcript" in capabilities
        assert "align" not in capabilities  # 仅英文 align 不覆盖中文

    def test_qwen3_align_covers_chinese(self, tmp_path) -> None:
        """R4-c：qwen3_align 覆盖中文，故默认注册表下中文素材能拿到 align（改进）。"""
        mgr = ModelPackManager(cache_dir=tmp_path)
        packs_zh = mgr.resolve_for_recipe("editorial_local", available_languages=("zh",))
        align_packs = [p for p in packs_zh if p.capability == "align"]
        assert len(align_packs) == 1
        assert align_packs[0].pack_id == "qwen3_align"  # 多语言对齐，不被 zh 过滤

    def test_missing_for_recipe_lists_uninstalled(self, tmp_path) -> None:
        """recipe 所需但未安装的 pack 被列出。

        R4-c：默认推荐链是 qwen3_asr + qwen3_align + nemo_sortformer（字母序优先）。
        """
        mgr = ModelPackManager(cache_dir=tmp_path)
        missing = mgr.missing_for_recipe("editorial_local", available_languages=("en",))
        missing_ids = {p.pack_id for p in missing}
        assert "qwen3_asr" in missing_ids
        assert "qwen3_align" in missing_ids
        assert "nemo_sortformer" in missing_ids

    def test_missing_shrinks_after_install(self, tmp_path) -> None:
        """安装一个 pack 后，missing 列表减少一项。"""
        mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        before = mgr.missing_for_recipe("editorial_local", available_languages=("en",))
        # 安装默认推荐的 qwen3_asr
        install_dir = tmp_path / "qwen3_asr"
        install_dir.mkdir()
        mgr.mark_installed("qwen3_asr", install_dir)
        after = mgr.missing_for_recipe("editorial_local", available_languages=("en",))
        assert len(after) == len(before) - 1
        assert "qwen3_asr" not in {p.pack_id for p in after}
