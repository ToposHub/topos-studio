"""B09: external composition references — identity, cycles, audits.

Covers the plan's acceptance shapes: A→B, A→B→C, the A→B→A cycle with a
full identity path, two references sharing one B (done-set reuse), depth
limits, pinned/follow policies and separated missing-file / missing-output
/ plugin-mismatch diagnostics.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tests.composite.standalone_gpu import run_standalone_gpu

from src.features.composite.core.external_comp_refs import (
    REF_CYCLE, REF_DEPTH_EXCEEDED, REF_MISSING_FILE, REF_MISSING_OUTPUT,
    REF_PLUGIN_MISMATCH, REF_SIZE_EXCEEDED, CompRef, RefDiagnostic,
    audit_external_refs, find_cycle, resolve_reference_graph,
)

pytestmark = pytest.mark.composite_core


def test_find_cycle_covers_acceptance_shapes():
    A, B, C = ("host", "A"), ("lib", "B"), ("lib2", "C")
    # A → B (acyclic).
    assert find_cycle(A, {A: (B,)}) == []
    # A → B → C (acyclic).
    assert find_cycle(A, {A: (B,), B: (C,)}) == []
    # A → B → A returns the full identity path.
    assert find_cycle(A, {A: (B,), B: (A,)}) == [A, B, A]


def test_resolution_orders_dependencies_first():
    A, B, C = ("p1", "A"), ("p2", "B"), ("p3", "C")
    result = resolve_reference_graph(A, {A: (B,), B: (C,)})
    assert result.ok
    assert result.order == (C, B, A)


def test_cycle_resolution_reports_identity_path():
    A, B = ("p1", "A"), ("p2", "B")
    result = resolve_reference_graph(A, {A: (B,), B: (A,)})
    assert not result.ok
    diagnostic, = result.diagnostics
    assert diagnostic.code == REF_CYCLE
    assert diagnostic.path == (A, B, A)
    assert "p1/A" in diagnostic.detail and "p2/B" in diagnostic.detail


def test_shared_subgraph_validated_once_and_reused():
    A1, A2, B = ("p1", "A1"), ("p1", "A2"), ("lib", "B")
    deps = {A1: (B,), A2: (B,)}
    validated: list = []

    def validate(key):
        validated.append(key)
        return None

    first = resolve_reference_graph(A1, deps, validate=validate)
    assert first.ok and first.order == (B, A1)
    # Second reference to the same B reuses the validated subgraph.
    second = resolve_reference_graph(A2, deps, validate=validate,
                                     done=set(first.order))
    assert second.ok
    assert second.reused == (B,)
    assert second.order == (A2,)
    # B validated exactly once across both resolutions.
    assert validated.count(B) == 1


def test_depth_limit_diagnoses_long_chains():
    deps = {}
    for i in range(40):
        deps[("p", str(i))] = (("p", str(i + 1)),)
    result = resolve_reference_graph(("p", "0"), deps, max_depth=8)
    assert not result.ok
    assert result.diagnostics[0].code == REF_DEPTH_EXCEEDED


def test_depth_limit_runs_before_recursion_limit():
    keys = [("project", str(index)) for index in range(1500)]
    deps = {parent: (child,) for parent, child in zip(keys, keys[1:])}
    result = resolve_reference_graph(keys[0], deps, max_depth=8)
    assert not result.ok
    assert result.diagnostics[0].code == REF_DEPTH_EXCEEDED
    assert result.diagnostics[0].key == keys[8]
    assert find_cycle(keys[0], deps) == []


def test_shared_tail_counts_toward_depth_regardless_of_traversal_order():
    a, b, c, d, e = [("project", key) for key in "ABCDE"]
    for children in ((d, b), (b, d)):
        deps = {a: children, b: (c,), c: (d,), d: (e,)}
        result = resolve_reference_graph(a, deps, max_depth=4, done={d, e})
        assert not result.ok
        assert result.diagnostics[0].code == REF_DEPTH_EXCEEDED


def test_size_limit_counts_unique_targets_before_validation():
    a, b, c, d = [("project", key) for key in "ABCD"]
    deps = {a: (b, c), b: (d,), c: (d,)}
    validated = []
    result = resolve_reference_graph(a, deps, max_nodes=3, validate=validated.append)
    assert not result.ok
    assert result.diagnostics[0].code == REF_SIZE_EXCEEDED
    assert validated == []
    result = resolve_reference_graph(a, deps, max_nodes=4, validate=validated.append)
    assert result.ok and result.order == (d, b, c, a)
    assert result.reused == (d,) and validated == [d, b, c, a]


def test_validation_failure_stops_resolution():
    A, B = ("p1", "A"), ("p2", "B")
    def validate(key):
        if key == B:
            return RefDiagnostic(REF_MISSING_FILE, key, "offline")
        return None
    result = resolve_reference_graph(A, {A: (B,)}, validate=validate)
    assert not result.ok
    assert result.diagnostics == (RefDiagnostic(REF_MISSING_FILE, B, "offline"),)
    assert result.order == ()


def test_comp_ref_identity_and_policies_round_trip():
    ref = CompRef(project_id="proj", comp_id="main",
                  revision_fingerprint="abc123",
                  relative_path="../libs/proj.topos",
                  update_policy="pinned")
    assert ref.key == ("proj", "main")
    assert CompRef.from_dict(ref.to_dict()) == ref
    # Follow-policy references track the target's current revision.
    follow = CompRef("proj", "main", update_policy="follow")
    assert follow.update_policy == "follow"
    assert CompRef.from_dict({"project_id": "p", "comp_id": "c",
                              "update_policy": "bogus"}) is None
    with pytest.raises(ValueError):
        CompRef("p", "c", update_policy="nope")


@pytest.mark.parametrize("path", ["/Users/me/file.topos", r"C:\projects\scene.topos",
                                  r"C:scene.topos", r"\\server\share\scene.topos",
                                  "file:///var/scene.topos", "bad\0path.topos"])
def test_comp_ref_refuses_nonportable_paths(path):
    with pytest.raises(ValueError, match="project-relative"):
        CompRef("proj", "main", relative_path=path)
    assert CompRef.from_dict({"project_id": "proj", "comp_id": "main",
                              "relative_path": path}) is None


def test_audit_separates_causes():
    refs = [
        CompRef("online", "main", relative_path="a.topos"),
        CompRef("nooutput", "main", relative_path="b.topos"),
        CompRef("badplugin", "main", relative_path="c.topos"),
        CompRef("offline", "main", relative_path="missing.topos"),
    ]
    payloads = {"online": {"graph": {}}, "nooutput": {"graph": {}},
                "badplugin": {"graph": {}}}

    def resolve_payload(ref):
        return payloads.get(ref.project_id)

    diagnostics = audit_external_refs(
        refs, resolve_payload,
        resolve_output=lambda ref: "out" if ref.project_id != "nooutput" else None,
        plugin_matches=lambda ref: ref.project_id != "badplugin")
    codes = {(d.code, d.key[0]) for d in diagnostics}
    assert codes == {
        (REF_MISSING_FILE, "offline"),
        (REF_MISSING_OUTPUT, "nooutput"),
        (REF_PLUGIN_MISMATCH, "badplugin"),
    }


def test_ok_reference_reports_no_diagnostics():
    ref = CompRef("online", "main")
    diagnostics = audit_external_refs([ref], lambda r: {"graph": {}})
    assert diagnostics == ()


def _package_with_graph(path, project_id, comp_id, graph):
    from src.shared.project.topos_project import ToposProject
    from src.shared.project.topos_project_io import ToposProjectIO

    project = ToposProject(project_id=project_id, name=project_id, project_mode="edit_project")
    assert ToposProjectIO.save(
        project, str(path), full_compositions_data={comp_id: {**graph.to_dict(), "fps": 24}},
    )


def _constant_comp(red):
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode

    graph = NodeGraph()
    source = ConstantNode("color")
    for name, value in {"width": 8, "height": 8, "color_r": red}.items():
        source.set_parameter(name, value)
    target = OutputNode("output")
    graph.add_node(source)
    graph.add_node(target)
    assert graph.connect(source.id, "result", target.id, "image")
    return graph


def _linked_comp(ref):
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.nodes.precomp_node import PrecompNode

    graph = NodeGraph()
    source = PrecompNode("ref")
    for key, value in ref.to_dict().items():
        source.set_parameter(f"ref_{key}", value)
    target = OutputNode("output")
    graph.add_node(source)
    graph.add_node(target)
    assert graph.connect(source.id, "result", target.id, "image")
    return graph


def test_external_worker_loads_nested_package_and_reports_cycles(tmp_path):
    from dataclasses import replace
    from src.features.composite.render.external_comp_snapshot import ExternalCompSnapshots

    leaf = tmp_path / "leaf.topos"
    child = tmp_path / "child.topos"
    _package_with_graph(leaf, "leaf", "scene", _constant_comp(0.25))
    _package_with_graph(child, "child", "scene", _linked_comp(CompRef(
        "leaf", "scene", relative_path=leaf.name, update_policy="follow",
    )))
    host_graph = _linked_comp(CompRef("child", "scene", relative_path=child.name))
    host = tmp_path / "host.topos"
    _package_with_graph(host, "host", "scene", host_graph)
    snapshot = replace(host_graph.snapshot(frame_number=25, fps=25), project_archive_path=str(host))
    session = ExternalCompSnapshots()
    flattened = session.expand(snapshot)
    assert not flattened.reference_diagnostics
    assert len(flattened.nodes) == 6
    leaf_id = next(nid for nid in flattened.nodes if nid.endswith("/color"))
    assert flattened.frame_overrides[leaf_id].apply(25) == 24
    assert len(snapshot.nodes) == 2

    _package_with_graph(leaf, "leaf", "scene", _linked_comp(CompRef(
        "child", "scene", relative_path=child.name,
    )))
    cycle = session.expand(snapshot).reference_diagnostics
    assert cycle[0]["code"] == REF_CYCLE
    assert cycle[0]["detail"] == "child/scene → leaf/scene → child/scene"


def test_external_render_freezes_revision_and_checks_pin(tmp_path) -> None:
    run_standalone_gpu(_check_external_render_freezes_revision_and_checks_pin, str(tmp_path))


def _check_external_render_freezes_revision_and_checks_pin(tmp_dir: str) -> None:
    from pathlib import Path

    tmp_path = Path(tmp_dir)
    from dataclasses import replace
    import numpy as np
    from src.features.composite.core.external_comp_refs import REF_REVISION_MISMATCH
    from src.features.composite.render.composite_export_renderer import CompositeExportRenderer
    from src.shared.project.resource_resolver import sha256_file

    moderngl = pytest.importorskip("moderngl")
    try:
        probe = moderngl.create_standalone_context()
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"No standalone GL context: {exc}")
    probe.release()

    child = tmp_path / "child.topos"
    _package_with_graph(child, "child", "scene", _constant_comp(0.25))
    fingerprint = sha256_file(str(child))
    host_graph = _linked_comp(CompRef(
        "child", "scene", relative_path=child.name, update_policy="follow",
    ))
    host = tmp_path / "host.topos"
    snapshot = replace(host_graph.snapshot(), project_archive_path=str(host))
    renderer = CompositeExportRenderer()
    try:
        initial = renderer.render_and_readback(snapshot)
        assert initial.error is None, initial.diagnostics
        assert initial.image.shape == (8, 8, 3)
        np.testing.assert_allclose(initial.image[..., 2], 0.25, atol=0.001)
        _package_with_graph(child, "child", "scene", _constant_comp(0.75))
        frozen = renderer.render_and_readback(snapshot)
        assert frozen.error is None
        np.testing.assert_allclose(frozen.image, initial.image, atol=0.001)
        followed = renderer.render_and_readback(snapshot, readback_output="srgb_u8")
        assert followed.error is None
        assert followed.image[0, 0, 2] > 200
        pinned = _linked_comp(CompRef("child", "scene", fingerprint, child.name)).snapshot()
        pinned = replace(pinned, project_archive_path=str(host))
        failed = renderer.render_and_readback(pinned, readback_output="srgb_u8")
        assert failed.diagnostics[0]["code"] == REF_REVISION_MISMATCH
    finally:
        renderer.cleanup()


def test_precomp_picker_commits_one_relocatable_pinned_reference(tmp_path, monkeypatch, qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QFileDialog, QInputDialog, QWidget
    from src.features.composite.core.external_comp_refs import set_reference_host_archive
    from src.features.composite.ui.external_comp_picker import ExternalCompPicker
    from src.shared.project.resource_resolver import sha256_file

    child = tmp_path / "lib" / "child.topos"
    child.parent.mkdir()
    _package_with_graph(child, "child", "scene", _constant_comp(0.25))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(child), ""))
    monkeypatch.setattr(
        QInputDialog, "getItem", lambda *args, **kwargs: (args[3][0], True),
    )
    set_reference_host_archive(str(tmp_path / "host.topos"))
    parent = QWidget()
    qtbot.addWidget(parent)
    commits = []
    warnings = []
    picker = ExternalCompPicker({}, commits.append, parent=parent)
    monkeypatch.setattr(picker, "_warning", warnings.append)
    parent.show()
    picker.show()
    try:
        qtbot.mouseClick(picker._choose, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: len(commits) == 1 or bool(warnings))
        assert not warnings
        assert commits[0] == {
            **CompRef("child", "scene", sha256_file(str(child)), "lib/child.topos").to_dict(),
            "output_name": "display",
        }
        qtbot.mouseClick(picker._unlink, Qt.MouseButton.LeftButton)
        assert commits[-1] == {}
    finally:
        set_reference_host_archive(None)


def test_handoff_embeds_a_nested_archive_closure_and_preserves_pins(tmp_path):
    from dataclasses import replace
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.render.external_comp_snapshot import (
        EXTERNAL_ARCHIVE_ROLE, ExternalCompSnapshots,
    )
    from src.shared.project.handoff import export_handoff_package
    from src.shared.project.resource_resolver import sha256_file
    from src.shared.project.topos_project_io import ToposProjectIO

    source_dir = tmp_path / "authoring"
    source_dir.mkdir()
    library = source_dir / "lib"
    library.mkdir()
    leaf = source_dir / "scene.topos"
    middle = library / "middle.topos"
    source = source_dir / "main.topos"
    _package_with_graph(leaf, "leaf", "scene", _constant_comp(0.25))
    leaf_sha = sha256_file(str(leaf))
    _package_with_graph(middle, "middle", "scene", _linked_comp(
        CompRef("leaf", "scene", leaf_sha, "../scene.topos"),
    ))
    middle_sha = sha256_file(str(middle))
    _package_with_graph(source, "main", "scene", _linked_comp(
        CompRef("middle", "scene", middle_sha, "lib/middle.topos"),
    ))

    moved = tmp_path / "moved.topos"
    handoff = export_handoff_package(str(source), str(moved))
    assert handoff.success, handoff.detail
    assert handoff.verdict == "full"
    leaf.unlink()
    middle.unlink()

    _, full_data = ToposProjectIO.load(str(moved))
    resolver = full_data["resource_resolver"]
    resources = resolver.index.entries_for_role(EXTERNAL_ARCHIVE_ROLE)
    assert {entry.sha256 for entry in resources} == {leaf_sha, middle_sha}
    graph = NodeGraph.from_dict(full_data["compositions"]["scene"])
    snapshot = replace(
        graph.snapshot(), project_archive_path=str(moved), external_archive_resolver=resolver,
    )
    frozen = ExternalCompSnapshots(freeze=True).expand(snapshot)
    assert not frozen.reference_diagnostics
    assert len(frozen.nodes) == 6
    assert any(node.node_type == "constant" for node in frozen.nodes.values())

    second = tmp_path / "handoff-again.topos"
    repacked = export_handoff_package(str(moved), str(second))
    assert repacked.success and repacked.verdict == "full"


@pytest.mark.parametrize("embedded_media", [False, True])
def test_handoff_embeds_only_portable_external_media(tmp_path, embedded_media):
    from dataclasses import replace
    from PIL import Image

    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.image_input_node import ImageInputNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.render.external_comp_snapshot import ExternalCompSnapshots
    from src.shared.project.handoff import export_handoff_package
    from src.shared.project.resource_resolver import ResourceIndex, make_resource_entry
    from src.shared.project.topos_project import ToposProject
    from src.shared.project.topos_project_io import ToposProjectIO

    media = tmp_path / "loose.png"
    Image.new("RGBA", (2, 2), (64, 0, 0, 128)).save(media)
    graph = NodeGraph()
    image = ImageInputNode("image")
    image.set_parameter("file_path", "topos-res://image" if embedded_media else media.name)
    output = OutputNode("output")
    graph.add_node(image)
    graph.add_node(output)
    assert graph.connect(image.id, "image", output.id, "image")
    child = tmp_path / "child.topos"
    source = tmp_path / "source.topos"
    if embedded_media:
        entry = make_resource_entry(str(media), resource_id="image", kind="image")
        assert ToposProjectIO.save(
            ToposProject(project_id="child", name="Child", project_mode="edit_project"),
            str(child),
            full_compositions_data={"scene": {**graph.to_dict(), "fps": 24}},
            resources_data=ResourceIndex(entries={entry.resource_id: entry}).to_dict(),
            resource_files={entry.archive_path: str(media)},
        )
    else:
        _package_with_graph(child, "child", "scene", graph)
    _package_with_graph(source, "source", "scene", _linked_comp(
        CompRef("child", "scene", relative_path=child.name),
    ))
    dest = tmp_path / "handoff.topos"

    handoff = export_handoff_package(str(source), str(dest))
    assert handoff.success, handoff.detail
    assert handoff.verdict == ("full" if embedded_media else "partial")
    if embedded_media:
        child.unlink()
        media.unlink()
    else:
        assert any(issue["object_kind"] == "external_comp" for issue in handoff.problems)
    _, full_data = ToposProjectIO.load(str(dest))
    if not embedded_media:
        assert "resource_resolver" not in full_data
        return
    host = NodeGraph.from_dict(full_data["compositions"]["scene"]).snapshot()
    expanded = ExternalCompSnapshots().expand(replace(
        host, project_archive_path=str(dest), external_archive_resolver=full_data["resource_resolver"],
    ))
    assert not expanded.reference_diagnostics
    image_path = next(ns.parameters["file_path"] for ns in expanded.nodes.values()
                      if ns.node_type == "image_input")
    with Image.open(image_path) as unpacked:
        assert unpacked.getpixel((0, 0)) == (64, 0, 0, 128)


def test_handoff_does_not_automatically_copy_over_budget_archive(tmp_path, monkeypatch):
    from src.shared.project import handoff as handoff_module
    from src.shared.project.topos_project_io import ToposProjectIO

    child = tmp_path / "child.topos"
    source = tmp_path / "source.topos"
    dest = tmp_path / "handoff.topos"
    _package_with_graph(child, "child", "scene", _constant_comp(0.25))
    _package_with_graph(source, "source", "scene", _linked_comp(
        CompRef("child", "scene", relative_path=child.name),
    ))
    monkeypatch.setattr(handoff_module, "_MAX_EXTRA_COMPOSITION_ARCHIVE_BYTES", 1)

    result = handoff_module.export_handoff_package(str(source), str(dest))
    assert result.success and result.verdict == "partial"
    _, full_data = ToposProjectIO.load(str(dest))
    assert "resource_resolver" not in full_data
