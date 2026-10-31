"""DESK-02A/02B · 工程字体与 LUT 的统一资源解析测试。

验收锚点：
- 02A：无预装字体机器（available_override 模拟）上，工程内嵌字体注册
  后字幕字体解析 available 且 from_project=True——不落 substitute；
  注册失败/损坏字体产生诊断而非崩溃；headless 可物化不注册。
- 02B：lut_path 持 ``topos-res://`` 引用时 load_lut 经当前工程资源上下文
  解析，与直接读外部文件逐值一致；无工程上下文/资源缺失受控失败；
  换工程后上下文切换（不跨工程串解析）。
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pytest

from tests.project.package_golden import golden_save_kwargs

from src.features.edit.core.captions.font_resolver import (
    FontResolver,
    ProjectFontRegistry,
)
from src.shared.color.lut_loader import load_lut
from src.shared.project.resource_resolver import (
    ProjectResourceResolver,
    ResourceIndex,
    make_resource_entry,
    set_active_project_resolver,
)
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.regression

_REPO_LUT = os.path.join(
    os.path.dirname(__file__), "..", "..", "luts", "cinematic.cube")


@pytest.fixture(autouse=True)
def _clean_project_context():
    yield
    set_active_project_resolver(None)
    FontResolver.set_available_override(None)
    FontResolver.set_project_families(None)


def _find_system_font() -> str | None:
    for pattern in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/*.ttf",
        "/System/Library/Fonts/*.ttc",
        "/usr/share/fonts/**/*.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]
    return None


def _build_resource_package(
    directory: str,
    filename: str,
    *,
    source_file: str,
    kind: str,
    resource_id: str,
    roles: tuple[str, ...],
) -> str:
    entry = make_resource_entry(
        source_file, resource_id=resource_id, kind=kind, roles=roles,
        original_filename=os.path.basename(source_file))
    index = ResourceIndex(entries={resource_id: entry})

    kwargs = golden_save_kwargs()
    project = kwargs.pop("project")
    kwargs["resources_data"] = index.to_dict()
    kwargs["resource_files"] = {entry.archive_path: source_file}
    filepath = os.path.join(directory, filename)
    assert ToposProjectIO.save(project, filepath, **kwargs)
    return filepath


class TestProjectFonts:
    def test_embedded_font_available_on_fontless_machine(self, qapp) -> None:
        """02A 主验收：模拟无预装字体机器，工程字体仍可用且不替换。"""
        font_file = _find_system_font()
        if font_file is None:
            pytest.skip("本机无可用的系统字体文件（无法构造注册向量）")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            package = _build_resource_package(
                tmp, "fonts.topos", source_file=font_file, kind="font",
                resource_id="res-font-1", roles=("caption_font",))
            _, full_data = ToposProjectIO.load(package)
            resolver = full_data["resource_resolver"]

            registry = ProjectFontRegistry.from_resources(resolver)
            assert registry.registrations, "字体资源未被扫描到"
            registration = registry.registrations[0]
            assert registration.registered, registration.detail
            family = registration.family
            assert family

            # 模拟"无预装字体机器"：系统字体库置空，仅工程字体在场
            FontResolver.set_available_override(frozenset())
            FontResolver.set_project_families(registry.families)

            result = FontResolver().resolve(family)
            assert result.available
            assert result.resolved == family
            assert not result.is_substitute
            assert result.from_project

    def test_corrupt_font_reports_diagnostic(self, qapp, tmp_path) -> None:
        font_file = _find_system_font()
        if font_file is None:
            pytest.skip("本机无可用的系统字体文件")
        bad = os.path.join(str(tmp_path), "broken.ttf")
        with open(bad, "wb") as fh:
            fh.write(b"not a real font file" * 16)

        package = _build_resource_package(
            str(tmp_path), "badfont.topos", source_file=bad, kind="font",
            resource_id="res-font-bad", roles=("caption_font",))
        _, full_data = ToposProjectIO.load(package)
        registry = ProjectFontRegistry.from_resources(
            full_data["resource_resolver"])
        assert registry.registrations[0].registered is False
        assert registry.diagnostics
        assert registry.families == frozenset()

    def test_headless_materializes_without_qt_registration(
        self, tmp_path,
    ) -> None:
        """无 QGuiApplication：可物化/校验内容，注册延后（诊断说明）。"""
        font_file = _find_system_font()
        if font_file is None:
            pytest.skip("本机无可用的系统字体文件")
        package = _build_resource_package(
            str(tmp_path), "hl.topos", source_file=font_file, kind="font",
            resource_id="res-font-hl", roles=("caption_font",))
        _, full_data = ToposProjectIO.load(package)
        registry = ProjectFontRegistry.from_resources(
            full_data["resource_resolver"], register_with_qt=False)
        registration = registry.registrations[0]
        assert registration.registered is False
        assert os.path.isfile(registration.local_path)  # 已物化
        assert registration.sha256  # 内容身份已登记

    def test_load_project_registers_fonts(self, qapp, tmp_path) -> None:
        """生产 EditProjectIO.load 链路完成注册（不因字体问题阻断打开）。"""
        font_file = _find_system_font()
        if font_file is None:
            pytest.skip("本机无可用的系统字体文件")
        from src.features.edit.core.edit_project_io import EditProjectIO

        package = _build_resource_package(
            str(tmp_path), "prod.topos", source_file=font_file, kind="font",
            resource_id="res-font-prod", roles=("caption_font",))
        io = EditProjectIO()
        loaded = io.load_project(package)
        assert loaded is not None
        assert FontResolver._project_families, "load 未注册工程字体"


class TestLutResourceResolution:
    def test_uri_lut_matches_direct_load(self, tmp_path) -> None:
        """02B 主验收：URI 引用的内嵌 LUT 与直读文件逐值一致。"""
        if not os.path.isfile(_REPO_LUT):
            pytest.skip("仓库 LUT 夹具缺失")
        package = _build_resource_package(
            str(tmp_path), "graded.topos", source_file=_REPO_LUT, kind="lut",
            resource_id="res-lut-1", roles=("lut",))
        _, full_data = ToposProjectIO.load(package)
        assert "resource_resolver" in full_data  # 上下文已激活

        via_uri = load_lut("topos-res://res-lut-1")
        direct = load_lut(_REPO_LUT)
        assert via_uri.size == direct.size
        assert np.array_equal(via_uri.table, direct.table)
        assert via_uri.source_path.endswith(".cube")

    def test_uri_without_project_context_fails_cleanly(self) -> None:
        set_active_project_resolver(None)
        with pytest.raises(FileNotFoundError, match="内嵌 LUT"):
            load_lut("topos-res://res-lut-9")

    def test_switching_projects_switches_context(self, tmp_path) -> None:
        """换工程后 URI 解析跟随新工程（同 id 不同内容不串包）。"""
        if not os.path.isfile(_REPO_LUT):
            pytest.skip("仓库 LUT 夹具缺失")
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        # 同 resource_id、不同内容（截断一半字节 → 不同的 LUT 表）
        lut_copy = os.path.join(str(dir_a), "variant.cube")
        with open(_REPO_LUT, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(lut_copy, "w", encoding="utf-8") as fh:
            fh.write(text)

        pkg_a = _build_resource_package(
            str(dir_a), "A.topos", source_file=_REPO_LUT, kind="lut",
            resource_id="res-lut-1", roles=("lut",))
        pkg_b = _build_resource_package(
            str(dir_a), "B.topos", source_file=lut_copy, kind="lut",
            resource_id="res-lut-1", roles=("lut",))

        ToposProjectIO.load(pkg_a)
        from src.shared.project.resource_resolver import (
            get_active_project_resolver,
        )
        resolver_a = get_active_project_resolver()
        ToposProjectIO.load(pkg_b)
        resolver_b = get_active_project_resolver()
        assert resolver_a is not resolver_b

        # 当前上下文 = 工程 B：同一 URI 解析到 B 的内容
        path_b = resolver_b.resolve_uri("topos-res://res-lut-1").local_path
        assert path_b == load_lut("topos-res://res-lut-1").source_path

        # 无资源索引的工程加载后：上下文清空（不残留上一工程）
        from tests.project.package_golden import save_golden_package
        plain = save_golden_package(str(dir_a), "plain.topos")
        ToposProjectIO.load(plain)
        assert get_active_project_resolver() is None

    def test_external_path_still_works(self) -> None:
        """外部路径直读行为不变（旧工程回归）。"""
        if not os.path.isfile(_REPO_LUT):
            pytest.skip("仓库 LUT 夹具缺失")
        lut = load_lut(_REPO_LUT)
        assert lut.size > 0
