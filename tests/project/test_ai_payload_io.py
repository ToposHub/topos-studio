"""T2.1 — ToposProjectIO AI payload 通道往返测试。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 2）：

- ``ToposProjectIO.save`` 接受 ``ai_session_data``/``ai_plans_data`` 并写入
  ``ai/session_catalog.json`` + ``ai/session.json`` + ``ai/plans.json`` + ``ai/jobs.json``。
- ``ToposProjectIO.load`` 读回 ``ai_session_catalog``/``ai_session``/``ai_plans``/``ai_jobs``。
- Phase B 往返：无 AI payload 的旧工程保存后不丢未知模块；有 AI payload 的工程
  被"不知 AI"的产品保存后 AI 模块仍保留。
"""

from __future__ import annotations

import json
import tempfile

import pytest

from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


def _read_zip_json(path: str, arc_name: str) -> dict | None:
    import zipfile
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        if arc_name not in names:
            return None
        return json.loads(zf.read(arc_name))


def _make_project() -> ToposProject:
    return ToposProject.create_single_asset(
        name="Test", asset_path="/tmp/test.jpg", media_type="image",
    )


class TestAIPayloadSaveLoad:
    def test_save_writes_ai_modules(self, tmp_path):
        """save 把 ai_session + ai_plans 写进对应 JSON 文件。"""
        zf_path = str(tmp_path / "p.topos")
        project = _make_project()
        session_data = {"version": 2, "sessions": {"s1": {"title": "会话1"}}}
        plans_data = {"schema_version": 1, "plans": [{"plan_id": "p1"}]}

        ok = ToposProjectIO.save(
            project, zf_path,
            ai_session_data=session_data,
            ai_plans_data=plans_data,
        )
        assert ok

        assert _read_zip_json(zf_path, "ai/session_catalog.json") == session_data
        # v1 session.json 也写（向后兼容）
        assert _read_zip_json(zf_path, "ai/session.json") is not None
        assert _read_zip_json(zf_path, "ai/plans.json") == plans_data

    def test_load_reads_ai_modules(self, tmp_path):
        """load 把 ai 模块读进 full_data。"""
        zf_path = str(tmp_path / "p.topos")
        project = _make_project()
        session_data = {"version": 2, "sessions": {}}
        plans_data = {"schema_version": 1, "plans": [{"plan_id": "p1"}]}
        ToposProjectIO.save(
            project, zf_path,
            ai_session_data=session_data, ai_plans_data=plans_data,
        )

        loaded_project, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert loaded_project is not None
        assert full_data is not None
        assert full_data["ai_session_catalog"] == session_data
        assert full_data["ai_plans"] == plans_data
        assert "ai_session" in full_data  # v1 兼容文件

    def test_save_and_load_job_checkpoint(self, tmp_path):
        """P7：JobRegistry 快照随工程保存，加载侧可安全恢复边界。"""
        zf_path = str(tmp_path / "jobs.topos")
        jobs_data = {
            "schema_version": "topos-job-registry/1.0",
            "jobs": [{"job_id": "job-1", "kind": "generation", "status": "running"}],
        }
        assert ToposProjectIO.save(_make_project(), zf_path, ai_jobs_data=jobs_data)
        assert _read_zip_json(zf_path, "ai/jobs.json") == jobs_data
        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data is not None
        assert full_data["ai_jobs"] == jobs_data

    def test_save_and_load_run_trace(self, tmp_path):
        """Run trace is a first-class AI module, not a dropped callback field."""
        zf_path = str(tmp_path / "trace.topos")
        trace_data = {
            "schema_version": 1,
            "sessions": {
                "s1": {
                    "next_seq": 1,
                    "dropped_event_count": 0,
                    "events": [{
                        "seq": 0,
                        "event_id": "e1",
                        "run_id": "r1",
                        "source": "assistant",
                        "kind": "run.created",
                        "timestamp": 1.0,
                        "payload": {},
                        "node_id": "",
                        "parent_id": "",
                    }],
                },
            },
        }
        assert ToposProjectIO.save(
            _make_project(), zf_path, ai_trace_data=trace_data,
        )
        assert _read_zip_json(zf_path, "ai/trace.json") == trace_data
        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data is not None
        assert full_data["ai_trace"] == trace_data

    def test_load_old_project_without_ai(self, tmp_path):
        """旧工程（无 ai/ 目录）load 不报错，full_data 不含 ai_* key。"""
        zf_path = str(tmp_path / "old.topos")
        ToposProjectIO.save(_make_project(), zf_path)  # 不传 ai payload

        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data is not None
        assert "ai_session_catalog" not in full_data
        assert "ai_plans" not in full_data

    def test_phase_b_preserves_ai_when_other_product_saves(self, tmp_path):
        """Phase B：第二个产品保存（不传 ai payload）时，已有 ai/ 模块仍保留。"""
        zf_path = str(tmp_path / "p.topos")
        project = _make_project()
        ToposProjectIO.save(
            project, zf_path,
            ai_session_data={"version": 2, "sessions": {"s1": {}}},
            ai_plans_data={"schema_version": 1, "plans": [{"plan_id": "p1"}]},
        )

        # 另一个产品保存（比如只更新 color，不碰 ai）
        ok = ToposProjectIO.save(
            project, zf_path,
            full_color_stacks_data={"cs1": {"stack_id": "cs1"}},
        )
        assert ok

        # ai 模块必须仍在
        assert _read_zip_json(zf_path, "ai/session_catalog.json") is not None
        assert _read_zip_json(zf_path, "ai/plans.json") == {"schema_version": 1, "plans": [{"plan_id": "p1"}]}

    def test_roundtrip_preserves_ai_payload(self, tmp_path):
        """完整往返：save(ai) → load → save(无ai) → load 仍能拿到 ai。"""
        zf_path = str(tmp_path / "p.topos")
        project = _make_project()
        original_session = {"version": 2, "sessions": {"s1": {"title": "T"}}}
        original_plans = {"schema_version": 1, "plans": [{"plan_id": "p1"}]}

        ToposProjectIO.save(
            project, zf_path,
            ai_session_data=original_session, ai_plans_data=original_plans,
        )

        # 第二次保存（模拟重新打开后保存，不显式传 ai）
        ok = ToposProjectIO.save(project, zf_path)
        assert ok

        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data["ai_plans"] == original_plans
        assert full_data["ai_session_catalog"] == original_session
