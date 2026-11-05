"""R02: LUT 资源注册、导入、迁移与重定位（LutResourceSession/validate）。

验收矩阵（计划 R02）：2³ identity、非 identity、1D shaper、非 0–1 DOMAIN、
损坏数据、非 ASCII 名、重名、读失败、取消；同内容多路径去重、同名不同内容
共存；工程保存/移动后重开引用一致；导入失败不发布半资源。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.shared.color.lut_resource_registry import (
    LutResourceError,
    LutResourceSession,
    validate_lut_file,
)


def _identity_cube(size: int = 2, title: str = "Identity") -> str:
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                lines.append(f"{r / (size - 1)} {g / (size - 1)} {b / (size - 1)}")
    return "\n".join(lines) + "\n"


def _channel_swap_cube(size: int = 2) -> str:
    """R 与 B 交换（通道交换 LUT，非 identity）。"""
    lines = [f"LUT_3D_SIZE {size}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                lines.append(f"{b / (size - 1)} {g / (size - 1)} {r / (size - 1)}")
    return "\n".join(lines) + "\n"


def _domain_cube(domain_min: float, domain_max: float) -> str:
    lines = [f"LUT_3D_SIZE 2",
             f"DOMAIN_MIN {domain_min} {domain_min} {domain_min}",
             f"DOMAIN_MAX {domain_max} {domain_max} {domain_max}"]
    for b in range(2):
        for g in range(2):
            for r in range(2):
                lines.append(f"{r} {g} {b}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def session(tmp_path: Path) -> LutResourceSession:
    return LutResourceSession(staging_root=str(tmp_path / "staging"))


def _write(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


# ═══ 验证矩阵 ═════════════════════════════════════════════════════════

def test_identity_cube_two_cubed_validates(tmp_path: Path) -> None:
    record = validate_lut_file(_write(tmp_path, "id2.cube", _identity_cube(2)))
    assert record.format == "cube"
    assert record.lut_size == 2
    assert record.domain_max == (1.0, 1.0, 1.0)
    assert not record.has_shaper


def test_non_identity_cube_validates(tmp_path: Path) -> None:
    record = validate_lut_file(_write(tmp_path, "swap.cube", _channel_swap_cube()))
    assert record.sha256  # 内容哈希登记
    # 通道交换表与 identity 哈希不同（同名也能区分）
    other = validate_lut_file(_write(tmp_path, "id2.cube", _identity_cube()))
    assert record.sha256 != other.sha256


def test_cube_with_1d_shaper_validates(tmp_path: Path) -> None:
    content = ["LUT_1D_SIZE 2", "0.0", "1.0", "LUT_3D_SIZE 2",
               *_identity_cube(2).splitlines()[1:]]
    record = validate_lut_file(_write(tmp_path, "shaper.cube", "\n".join(content)))
    assert record.has_shaper


def test_non_unit_domain_cube_validates(tmp_path: Path) -> None:
    record = validate_lut_file(_write(tmp_path, "hdr.cube", _domain_cube(-0.25, 1.5)))
    assert record.domain_min == (-0.25, -0.25, -0.25)
    assert record.domain_max == (1.5, 1.5, 1.5)


def test_corrupted_data_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LutResourceError) as info:
        validate_lut_file(_write(tmp_path, "broken.cube", "LUT_3D_SIZE 4\n0 0 0\n"))
    assert info.value.code.value == "parse_failed"


def test_non_finite_values_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(LutResourceError) as info:
        # NaN 不在行首（行首字母会被当作关键字行），藏在数值位以覆盖解析后检查
        validate_lut_file(_write(tmp_path, "nan.cube", "LUT_3D_SIZE 2\n0 nan 0\n" + "0 0 0\n" * 7))
    assert info.value.code.value == "non_finite_values"


def test_invalid_domain_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LutResourceError) as info:
        validate_lut_file(_write(tmp_path, "rev.cube", _domain_cube(1.0, 0.0)))
    assert info.value.code.value == "invalid_domain"


def test_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "lut.csp", "hello")
    with pytest.raises(LutResourceError) as info:
        validate_lut_file(path)
    assert info.value.code.value == "unsupported_format"


def test_read_failure_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LutResourceError) as info:
        validate_lut_file(str(tmp_path / "missing.cube"))
    assert info.value.code.value == "read_failed"


def test_cancellation_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "id2.cube", _identity_cube())
    with pytest.raises(LutResourceError) as info:
        validate_lut_file(path, cancel_check=lambda: True)
    assert info.value.code.value == "cancelled"


def test_non_ascii_filename_roundtrip(tmp_path: Path) -> None:
    record = validate_lut_file(_write(tmp_path, "电影胶片.cube", _identity_cube(title="电影")))
    assert record.original_filename == "电影胶片.cube"
    assert record.title == "电影"
    session = LutResourceSession(staging_root=str(tmp_path / "st"))
    result = session.import_files([record.source_path])[0]
    assert result.status == "imported"
    assert result.entry is not None and record.sha256 in result.entry.archive_path


# ═══ 注册/去重/迁移 ═══════════════════════════════════════════════════

def test_same_content_multiple_paths_deduplicated(tmp_path: Path,
                                                  session: LutResourceSession) -> None:
    first = _write(tmp_path / "a", "one.cube", _identity_cube())
    second = _write(tmp_path / "b", "two.cube", _identity_cube())
    results = session.import_files([first, second])
    assert [r.status for r in results] == ["imported", "duplicate"]
    assert results[0].entry.resource_id == results[1].entry.resource_id
    assert len(session.lut_entries()) == 1


def test_newly_imported_uri_loads_from_verified_staging_until_reset(
        tmp_path: Path, session: LutResourceSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.shared.color import lut_resource_registry
    from src.shared.color.lut_loader import load_lut

    source = _write(tmp_path, 'swap.cube', _channel_swap_cube())
    entry = session.import_files([source])[0].entry
    assert entry is not None
    monkeypatch.setattr(lut_resource_registry, 'get_lut_resource_session',
                        lambda: session)
    Path(source).unlink()  # Preview must use the validated staged copy.
    assert session.staged_path(entry.uri) is not None
    assert session.staged_path(entry.content_uri) is not None
    lut = load_lut(entry.uri)
    assert lut.size == 2
    session.reset()
    assert session.staged_path(entry.uri) is None
    with pytest.raises(FileNotFoundError):
        load_lut(entry.uri)


def test_same_name_different_content_coexist(tmp_path: Path,
                                             session: LutResourceSession) -> None:
    first = _write(tmp_path, "grain.cube", _identity_cube())
    second = _write(tmp_path / "other", "grain.cube", _channel_swap_cube())
    results = session.import_files([first, second])
    assert [r.status for r in results] == ["imported", "imported"]
    ids = {r.entry.resource_id for r in results}
    assert len(ids) == 2
    paths = {r.entry.archive_path for r in results}
    assert len(paths) == 2  # 内容寻址分路径


def test_import_failure_publishes_no_entry(tmp_path: Path,
                                           session: LutResourceSession) -> None:
    good = _write(tmp_path, "ok.cube", _identity_cube())
    bad = _write(tmp_path, "bad.cube", "LUT_3D_SIZE 3\nnope\n")
    results = session.import_files([good, bad])
    assert [r.status for r in results] == ["imported", "failed"]
    assert results[1].error is not None
    uris = {e.uri for e in session.lut_entries()}
    assert len(uris) == 1  # 失败不产生半条目


def test_import_cancelled_publishes_no_entry(tmp_path: Path,
                                             session: LutResourceSession) -> None:
    path = _write(tmp_path, "id.cube", _identity_cube())
    result = session.import_files([path], cancel_check=lambda: True)[0]
    assert result.status == "cancelled"
    assert session.lut_entries() == []


def test_stage_copy_failure_leaves_no_entry(tmp_path: Path) -> None:
    # 暂存目录不可写 → 拷贝失败 → 无条目（磁盘满/权限场景）
    path = _write(tmp_path, "id.cube", _identity_cube())
    session = LutResourceSession(staging_root=str(tmp_path / "file.txt"))
    (tmp_path / "file.txt").write_text("occupies the name")
    result = session.import_files([path])[0]
    assert result.status == "failed"
    assert result.error is not None and result.error.code.value == "write_failed"
    assert session.lut_entries() == []


def test_migrate_legacy_path_registers_and_keeps_missing_visible(
        tmp_path: Path, session: LutResourceSession) -> None:
    legacy = _write(tmp_path, "legacy.cube", _identity_cube())
    uri, status = session.migrate_legacy(legacy)
    assert status == "migrated" and uri and uri.startswith("topos-res://")
    assert session.status_of(uri) == "registered"
    # 缺失文件：明确 missing，不静默移除
    uri2, status2 = session.migrate_legacy(str(tmp_path / "gone.cube"))
    assert uri2 is None and status2 == "missing"
    assert session.status_of(str(tmp_path / "gone.cube")) == "missing"
    # 已是资源引用 → 原样返回
    assert session.migrate_legacy(uri) == (uri, "already_reference")


# ═══ 保存载荷与工程往返 ═══════════════════════════════════════════════

def test_imported_luts_survive_save_and_relocate(tmp_path: Path) -> None:
    import shutil
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project import EditProject
    from src.shared.color.lut_resource_registry import get_lut_resource_session

    lut_path = _write(tmp_path, "keep.cube", _identity_cube())
    session = get_lut_resource_session()
    session.reset()
    assert session.import_files([lut_path])[0].status == "imported"

    project = EditProject.create(name="lut-persist")
    target = tmp_path / "origin.topos"
    assert EditProjectIO().save_project(
        target, project, create_backup=False, use_ai_callback=False)

    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        index_json = json.loads(zf.read("resources/resources.json"))
        assert any(n.startswith("resources/lut/") for n in names)
        assert index_json["resources"][0]["kind"] == "lut"
        sha = index_json["resources"][0]["sha256"]

    # 「移动整个工程到另一目录/机器」：拷贝包文件重开，引用仍按内容解析
    moved = tmp_path / "moved" / "relocated.topos"
    moved.parent.mkdir()
    shutil.copy(target, moved)
    session.reset()
    loaded = EditProjectIO().load_project(moved)
    assert loaded is not None and loaded[0] is not None
    # load 后会话绑定工程索引：同 sha 资源可解析
    entries = get_lut_resource_session().lut_entries()
    assert any(e.sha256 == sha for e in entries), entries
    # 原始源文件删除后，包内资源仍可解析（机器路径无关）
    Path(lut_path).unlink()
    from src.shared.project.resource_resolver import get_active_project_resolver
    active = get_active_project_resolver()
    assert active is not None
    local = active.resolve_uri(f"topos-res://lut_{sha[:32]}").local_path
    assert Path(local).is_file()
    from src.shared.project.resource_resolver import set_active_project_resolver
    get_lut_resource_session().reset()
    set_active_project_resolver(None)


def test_empty_session_keeps_none_save_semantics(tmp_path: Path) -> None:
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project import EditProject
    from src.shared.color.lut_resource_registry import get_lut_resource_session
    # 顺序污染防御：save_project 取全局 LUT/bake 会话的 save_payload 决定
    # 是否写 resources 模块（edit_project_io.py:1273-1299）；先前的
    # load_project 测试会把包索引（含 bake 包 generated 条目）合并进全局
    # LUT 会话且未必清理，先复位全局会话再验证"空会话"语义。
    from src.features.color.core.color_bake_result_registry import (
        get_bake_result_session,
    )
    get_lut_resource_session().reset()
    get_bake_result_session().reset()
    session = LutResourceSession(staging_root=str(tmp_path / "empty"))
    assert session.save_payload() == (None, None)
    # 无资源协议保存的前提是**全部**资源会话为空（R06-B 起 bake 结果
    # 会话同语义并入保存载荷——任一会话有条目即写资源索引）；显式
    # reset 全局 bake 会话，防同进程早前用例注册的条目改变前提。
    from src.features.color.core.color_bake_result_registry import (
        get_bake_result_session,
    )
    get_bake_result_session().reset()
    target = tmp_path / "plain.topos"
    project = EditProject.create(name="plain")
    assert EditProjectIO().save_project(
        target, project, create_backup=False, use_ai_callback=False)
    with zipfile.ZipFile(target) as zf:
        assert "resources/resources.json" not in zf.namelist()
