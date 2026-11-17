"""B6：批改名 preview/execute 计划确认协议。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.transactions import FileOperationService
from src.features.files.transactions.batch_plan import (
    BatchRenamePlanner,
    PlanError,
)


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    service = FileOperationService(catalog)
    planner = BatchRenamePlanner(catalog, service)
    yield tmp_path, catalog, library, lib, planner, service
    catalog.close()


def _seed(env, name: str, *, rating: int = 3) -> Path:
    tmp, catalog, library, lib, planner, service = env
    asset = lib / name
    asset.write_bytes(b"payload")
    catalog.upsert_asset(library.library_id, asset, size=7)
    catalog.patch_user_fields(library.library_id, asset, rating=rating)
    return asset


# ---------------------------------------------------------------- preview


def test_preview_returns_plan_id_version_and_identities(env):
    tmp, catalog, library, lib, planner, service = env
    a = _seed(env, "IMG_001.png")
    b = _seed(env, "IMG_002.png")
    plan = planner.preview(library.library_id, [a, b], "sequence",
                           {"prefix": "shoot-", "start": 1, "width": 2})
    assert plan.plan_id and plan.version == 1
    assert [i.proposed_name for i in plan.items] == [
        "shoot-01.png", "shoot-02.png"]
    assert set(plan.identities) == {str(a), str(b)}
    assert not plan.has_conflicts


def test_preview_flags_conflicts_within_batch_and_disk(env):
    tmp, catalog, library, lib, planner, service = env
    _seed(env, "a.png")
    occupied = _seed(env, "b.png")
    # replace 到同名 → 批内重名
    plan = planner.preview(library.library_id, [occupied, occupied.parent / "a.png"],
                           "replace", {"find": "a", "replace": "b"})
    conflicting = [i for i in plan.items if i.conflict]
    assert conflicting and conflicting[0].reason == "批内目标重名"
    # 磁盘已存在目标
    plan2 = planner.preview(library.library_id, [occupied], "replace",
                            {"find": "b", "replace": "a"})
    assert plan2.items[0].conflict and plan2.items[0].reason == "目标已存在于磁盘"


def test_template_mode_uses_asset_metadata(env):
    tmp, catalog, library, lib, planner, service = env
    asset = _seed(env, "clip.png")
    catalog.upsert_asset(library.library_id, asset, camera="Topos Cam",
                         date_taken=1730000000.0)
    plan = planner.preview(library.library_id, [asset], "template",
                           {"template": "{camera}_{date}_{seq}.{ext}"})
    name = plan.items[0].proposed_name
    assert name.startswith("Topos Cam_") and name.endswith("_001.png")


def test_preview_rejects_bad_inputs(env):
    tmp, catalog, library, lib, planner, service = env
    with pytest.raises(PlanError) as info:
        planner.preview(library.library_id, [], "sequence", {})
    assert info.value.code == "EMPTY"
    with pytest.raises(PlanError) as info:
        planner.preview(library.library_id, [lib / "ghost"], "sequence", {})
    assert info.value.code == "SOURCE_MISSING"
    with pytest.raises(PlanError) as info:
        planner.preview(library.library_id, [lib], "format_disk", {})
    assert info.value.code == "BAD_MODE"


# ---------------------------------------------------------------- execute


def test_execute_applies_recorded_pairs_with_propagation(env):
    tmp, catalog, library, lib, planner, service = env
    a = _seed(env, "IMG_001.png", rating=5)
    b = _seed(env, "IMG_002.png", rating=4)
    plan = planner.preview(library.library_id, [a, b], "sequence",
                           {"prefix": "final-", "start": 10, "width": 3})
    outcomes = planner.execute(plan.plan_id, library.library_id)
    assert all(o.ok for o in outcomes), [(o.status, o.code) for o in outcomes]
    assert (lib / "final-010.png").exists() and (lib / "final-011.png").exists()
    assert not a.exists() and not b.exists()
    assert catalog.asset(library.library_id, lib / "final-010.png")["rating"] == 5
    assert catalog.asset(library.library_id, a) is None


def test_execute_rejects_stale_sources(env):
    """预览后源变化 → 整体拒绝重预览，绝不现场重算执行。"""
    tmp, catalog, library, lib, planner, service = env
    a = _seed(env, "a.png")
    plan = planner.preview(library.library_id, [a], "replace",
                           {"find": "a", "replace": "z"})
    time.sleep(0.01)
    a.write_bytes(b"changed after preview")  # size/mtime 指纹失效
    with pytest.raises(PlanError) as info:
        planner.execute(plan.plan_id, library.library_id)
    assert info.value.code == "STALE_PLAN"
    assert a.exists() and (lib / "z.png").exists() is False, "什么都没被改名"


def test_execute_rejects_unknown_and_consumed_plans(env):
    tmp, catalog, library, lib, planner, service = env
    with pytest.raises(PlanError) as info:
        planner.execute("no-such-plan", library.library_id)
    assert info.value.code == "PLAN_UNKNOWN"
    a = _seed(env, "b.png")
    plan = planner.preview(library.library_id, [a], "replace",
                           {"find": "b", "replace": "c"})
    assert planner.execute(plan.plan_id, library.library_id)[0].ok
    with pytest.raises(PlanError) as info:
        planner.execute(plan.plan_id, library.library_id)
    assert info.value.code == "PLAN_CONSUMED"
    assert (lib / "c.png").exists() and not (lib / "c 2.png").exists()


def test_execute_rejects_conflicting_plan(env):
    tmp, catalog, library, lib, planner, service = env
    _seed(env, "a.png")
    _seed(env, "b.png")
    plan = planner.preview(library.library_id, [lib / "a.png"], "replace",
                           {"find": "a", "replace": "b"})
    assert plan.has_conflicts
    with pytest.raises(PlanError) as info:
        planner.execute(plan.plan_id, library.library_id)
    assert info.value.code == "PLAN_HAS_CONFLICTS"
    assert (lib / "a.png").exists() and (lib / "b.png").exists()
