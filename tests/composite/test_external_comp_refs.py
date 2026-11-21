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

from src.features.composite.core.external_comp_refs import (
    REF_CYCLE, REF_DEPTH_EXCEEDED, REF_MISSING_FILE, REF_MISSING_OUTPUT,
    REF_OK, REF_PLUGIN_MISMATCH, CompRef, RefDiagnostic,
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
