"""PL-07 回归：Files 启动走统一命令服务（prepare 冻结 → dispatch 提交）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.features.files.plugin_launch_service import (
    LAUNCHABLE_LEGACY_IDS,
    LegacyFilesCommandService,
)
from src.shared.plugins.services import CommandRejected, PreparedInvocation


@dataclass
class _Info:
    plugin_id: str
    can_launch: bool = True
    unavailable: str = ""


class _Catalog:
    def __init__(self, infos) -> None:
        self._infos = infos

    def snapshot(self):
        return tuple(self._infos)

    def launch_request(self, plugin_id, paths):
        info = next((i for i in self._infos if i.plugin_id == plugin_id), None)
        if info is None:
            raise ValueError(f"未知插件：{plugin_id}")
        if not info.can_launch:
            raise ValueError(info.unavailable or "当前不可启动")
        if plugin_id == "format_factory" and len(paths) > 1:
            raise ValueError("格式工厂的现有启动接口一次只能接收一个文件，请只选择一个素材")
        if plugin_id == "format_factory":
            return f"/api/plugin/{plugin_id}/launch", {"path": paths[0] if paths else ""}
        if plugin_id == "decode_benchmark":
            return f"/api/plugin/{plugin_id}/launch", {"paths": list(paths)}
        return f"/api/plugin/{plugin_id}/launch", {}


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def launch_plugin(self, plugin_id, endpoint, payload):
        self.calls.append((plugin_id, endpoint, payload))


def _service(infos=None) -> tuple[LegacyFilesCommandService, _Transport]:
    infos = infos if infos is not None else [_Info(pid) for pid in LAUNCHABLE_LEGACY_IDS]
    transport = _Transport()
    return LegacyFilesCommandService(_Catalog(infos), transport), transport


def test_only_verified_launch_routes_are_commands():
    service, _ = _service([_Info(pid) for pid in (*LAUNCHABLE_LEGACY_IDS, "code_editor")])
    assert "code_editor" not in service.command_ids()
    assert set(service.command_ids()) == LAUNCHABLE_LEGACY_IDS


def test_prepare_freezes_selection_and_payload_kind():
    service, _ = _service()
    prepared = service.prepare("decode_benchmark", ["/a.mov", "", "/b.mov"])
    assert prepared.context.entry_paths == ("/a.mov", "/b.mov")   # 空串被剔除并冻结
    assert prepared.to_payload() == {"paths": ["/a.mov", "/b.mov"]}
    single = service.prepare("format_factory", ["/only.mov"])
    assert single.payload == {"path": "/only.mov"}
    none = service.prepare("image_factory", [])
    assert none.payload == {}


def test_prepare_rejects_multi_for_single_file_tool():
    service, _ = _service()
    with pytest.raises(CommandRejected) as exc:
        service.prepare("format_factory", ["/a.mov", "/b.mov"])
    assert "一个文件" in str(exc.value)
    assert exc.value.error_code == "COMMAND_NOT_APPLICABLE"


def test_prepare_rejects_unavailable_or_unknown():
    service, _ = _service([_Info("image_factory", can_launch=False,
                                 unavailable="插件已停用，重启后生效")])
    with pytest.raises(CommandRejected, match="插件已停用"):
        service.prepare("image_factory", [])
    with pytest.raises(CommandRejected, match="没有受支持"):
        service.prepare("ghost", [])


def test_dispatch_submits_frozen_request_and_reports_accepted_only():
    service, transport = _service()
    prepared = service.prepare("shutdown_timer", [])
    result = service.dispatch(prepared)
    assert transport.calls == [("shutdown_timer", "/api/plugin/shutdown_timer/launch", {})]
    assert result.accepted and result.stage == "queued"
    assert "工具窗口由插件创建" in result.message                 # 受理≠窗口已开
    assert result.invocation_id == prepared.context.invocation_id


def test_finish_translates_transport_acknowledgement():
    service, _ = _service()
    prepared = service.prepare("image_factory", [])
    ok = service.finish(prepared, True, "已提交打开请求；工具窗口由插件创建。")
    assert ok.ok and ok.stage == "dispatched"
    failed = service.finish(prepared, False, "插件未接受打开请求，请检查插件状态或重试。")
    assert failed.ok is False and failed.error_code == "LAUNCH_NOT_ACCEPTED"
    assert failed.invocation_id == prepared.context.invocation_id


def test_prepared_payload_is_immutable():
    service, _ = _service()
    prepared = service.prepare("format_factory", ["/a.mov"])
    with pytest.raises(TypeError):
        prepared.payload["path"] = "/evil"                     # type: ignore[index]


def test_legacy_selection_paths_must_be_strings():
    service, _ = _service()

    class _NotStr:
        pass

    with pytest.raises(CommandRejected) as exc:
        service.prepare("format_factory", [_NotStr()])          # 非字符串要拒绝，不静默丢弃
    assert exc.value.error_code == "INVALID_SELECTION"


def test_nested_prepared_payload_cannot_change_the_frozen_selection():
    service, transport = _service()
    prepared = service.prepare("decode_benchmark", ["/a.mov"])
    assert prepared.payload["paths"] == ("/a.mov",)
    detached = prepared.to_payload()
    detached["paths"].append("/other.mov")
    service.dispatch(prepared)
    assert transport.calls[0][2] == {"paths": ["/a.mov"]}
    transport.calls[0][2]["paths"].append("/transport-only.mov")
    assert prepared.context.entry_paths == ("/a.mov",)
    assert prepared.to_payload() == {"paths": ["/a.mov"]}


@pytest.mark.parametrize("paths", ["/one.mov", b"/one.mov"])
def test_selection_does_not_treat_a_path_as_an_iterable_of_characters(paths):
    service, _ = _service()
    with pytest.raises(CommandRejected) as exc:
        service.prepare("decode_benchmark", paths)
    assert exc.value.error_code == "INVALID_SELECTION"


def test_prepare_rejects_unverified_route_even_when_catalog_claims_it_exists():
    service, _ = _service([_Info("third_party")])
    with pytest.raises(CommandRejected, match="没有受支持"):
        service.prepare("third_party", [])


@pytest.mark.parametrize("reason", ["cancelled", "expired", "identity"])
def test_dispatch_rejects_cancelled_expired_or_mismatched_invocations(reason):
    from dataclasses import replace
    service, transport = _service()
    prepared = service.prepare("image_factory", [])
    if reason == "cancelled":
        prepared.context.cancel_token.request()
    elif reason == "expired":
        prepared = replace(prepared, context=replace(prepared.context, deadline=0.0))
    else:
        prepared = replace(prepared, plugin_id="files.shutdown_timer")
    result = service.dispatch(prepared)
    assert not result.accepted and not result.ok
    assert transport.calls == []


# ---------------------------------------------------------------------------
# PR-05 回归：执行前权威复核与一致的受理结果
# ---------------------------------------------------------------------------

class _CountingCatalog(_Catalog):
    def __init__(self, infos) -> None:
        super().__init__(infos)
        self.launch_requests = 0

    def launch_request(self, plugin_id, paths):
        self.launch_requests += 1
        return super().launch_request(plugin_id, paths)


def test_dispatch_rejects_when_disabled_between_prepare_and_dispatch():
    """prepare 后被停用：dispatch（未复核）必须拒绝，不能照旧提交。"""
    service, transport = _service()
    prepared = service.prepare("image_factory", [])
    service.catalog._infos = (_Info("image_factory", can_launch=False,
                                    unavailable="插件已停用，重启后生效"),)
    result = service.dispatch(prepared)
    assert not result.accepted and not result.ok
    assert result.error_code == "COMMAND_NOT_APPLICABLE"
    assert "已停用" in result.message
    assert transport.calls == []                            # 从未提交 HTTP


def test_revalidate_rejects_stale_state_and_marks_prepared():
    service, transport = _service()
    prepared = service.prepare("image_factory", [])
    checked = service.revalidate(prepared)
    assert checked.revalidated is True                      # 复核通过：可交 dispatch
    assert checked.payload == prepared.payload
    assert transport.calls == []
    service.catalog._infos = (_Info("image_factory", can_launch=False,
                                    unavailable="插件已停用，重启后生效"),)
    with pytest.raises(CommandRejected) as exc:
        service.revalidate(prepared)                        # 停用后复核：响亮拒绝
    assert exc.value.error_code == "COMMAND_NOT_APPLICABLE"


def test_revalidated_dispatch_submits_without_rescanning():
    """worker 复核过的调用，dispatch 不再重复扫描（GUI 线程只做提交）。"""
    catalog = _CountingCatalog([_Info(pid) for pid in LAUNCHABLE_LEGACY_IDS])
    transport = _Transport()
    service = LegacyFilesCommandService(catalog, transport)
    prepared = service.revalidate(service.prepare("shutdown_timer", []))
    assert catalog.launch_requests == 2                     # prepare + revalidate 各一次
    result = service.dispatch(prepared)
    assert result.accepted and result.stage == "queued"
    assert catalog.launch_requests == 2                     # dispatch 没有补扫描
    assert len(transport.calls) == 1


def test_transport_sync_rejection_is_never_reported_as_accepted():
    class _SyncRefusal:
        def launch_plugin(self, plugin_id, endpoint, payload):
            return False

    class _BrokenTransport:
        def launch_plugin(self, plugin_id, endpoint, payload):
            raise OSError("connection refused")

    service = LegacyFilesCommandService(_Catalog([_Info("image_factory")]), _SyncRefusal())
    result = service.dispatch(service.revalidate(service.prepare("image_factory", [])))
    assert not result.accepted and result.error_code == "LAUNCH_REJECTED"
    assert result.retriable is True                         # 同步拒绝可安全重试
    broken = LegacyFilesCommandService(_Catalog([_Info("image_factory")]), _BrokenTransport())
    result = broken.dispatch(broken.revalidate(broken.prepare("image_factory", [])))
    assert not result.accepted and result.error_code == "LAUNCH_REJECTED"
    assert result.retriable is True


def test_stage_vocabulary_separates_queued_dispatched_and_completed():
    from src.shared.plugins.services import STAGES
    assert "queued" in STAGES and "dispatched" in STAGES and "completed" in STAGES
    service, _ = _service()
    prepared = service.prepare("image_factory", [])
    assert service.dispatch(prepared).stage == "queued"       # 仅发出请求，尚无 HTTP ACK
    assert service.finish(prepared, True, "ok").stage == "dispatched"  # ACK ≠ 作业完成
    assert service.finish(prepared, False, "no").error_code == "LAUNCH_NOT_ACCEPTED"


@pytest.mark.parametrize("copy_check", [False, True])
def test_caller_cannot_forge_a_service_revalidation(copy_check):
    from dataclasses import replace
    service, transport = _service()
    prepared = service.prepare("image_factory", [])
    prepared = service.revalidate(prepared) if copy_check else prepared
    forged = replace(prepared, revalidated=True)
    service.catalog._infos = (_Info("image_factory", can_launch=False),)
    result = service.dispatch(forged)
    assert not result.accepted and result.error_code == "COMMAND_NOT_APPLICABLE"
    assert result.invocation_id == forged.context.invocation_id
    assert transport.calls == []


def test_revalidation_is_bound_to_the_authoritative_service():
    first, _ = _service()
    checked = first.revalidate(first.prepare("image_factory", []))
    second, transport = _service([_Info("image_factory", can_launch=False)])
    result = second.dispatch(checked)
    assert not result.accepted and transport.calls == []


# ---------------------------------------------------------------------------
# F3：库 revision / deadline 复核——重扫切库后的过期调用拒绝重发
# ---------------------------------------------------------------------------

class _RevisionSource:
    def __init__(self, revision: int) -> None:
        self.revision = revision


def test_f3_prepare_freezes_library_revision_and_deadline():
    transport = _Transport()
    service = LegacyFilesCommandService(
        _Catalog([_Info("image_factory")]), transport,
        revision_provider=lambda: 42)
    prepared = service.prepare("image_factory", ["/m/a.png"])
    assert prepared.context.entry_revision == 42
    assert prepared.context.deadline is not None and prepared.context.deadline > 0


def test_f3_revalidate_rejects_when_library_revised():
    source = _RevisionSource(7)
    transport = _Transport()
    service = LegacyFilesCommandService(
        _Catalog([_Info("image_factory")]), transport,
        revision_provider=lambda: source.revision)
    prepared = service.prepare("image_factory", ["/m/a.png"])
    source.revision = 9                    # 手势后重扫/切库
    with pytest.raises(CommandRejected) as exc:
        service.revalidate(prepared)
    assert exc.value.error_code == "LIBRARY_REVISED"
    assert exc.value.retriable


def test_f3_dispatch_rejects_revision_drift_after_revalidate():
    source = _RevisionSource(7)
    transport = _Transport()
    service = LegacyFilesCommandService(
        _Catalog([_Info("image_factory")]), transport,
        revision_provider=lambda: source.revision)
    prepared = service.prepare("image_factory", ["/m/a.png"])
    checked = service.revalidate(prepared)
    source.revision = 8                    # 复核→提交之间漂移
    outcome = service.dispatch(checked)
    assert not outcome.accepted and outcome.error_code == "LIBRARY_REVISED"
    assert transport.calls == []           # 过期调用不得发出


def test_f3_no_revision_provider_skips_check_without_pretending():
    transport = _Transport()
    service = LegacyFilesCommandService(_Catalog([_Info("image_factory")]), transport)
    prepared = service.prepare("image_factory", ["/m/a.png"])
    assert prepared.context.entry_revision == 0
    checked = service.revalidate(prepared)    # 0 = 宿主未提供：跳过不冒装
    assert service.dispatch(checked).accepted


def test_f3_revision_none_from_provider_treated_as_unavailable():
    transport = _Transport()
    service = LegacyFilesCommandService(
        _Catalog([_Info("image_factory")]), transport,
        revision_provider=lambda: None)    # facade 未就绪
    prepared = service.prepare("image_factory", ["/m/a.png"])
    assert prepared.context.entry_revision == 0
    assert service.dispatch(service.revalidate(prepared)).accepted
