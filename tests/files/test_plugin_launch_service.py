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
    assert prepared.payload == {"paths": ["/a.mov", "/b.mov"]}
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
    with pytest.raises(CommandRejected, match="未知插件"):
        service.prepare("ghost", [])


def test_dispatch_submits_frozen_request_and_reports_accepted_only():
    service, transport = _service()
    prepared = service.prepare("shutdown_timer", [])
    result = service.dispatch(prepared)
    assert transport.calls == [("shutdown_timer", "/api/plugin/shutdown_timer/launch", {})]
    assert result.accepted and result.stage == "dispatched"
    assert "工具窗口由插件创建" in result.message                 # 受理≠窗口已开
    assert result.invocation_id == prepared.context.invocation_id


def test_finish_translates_transport_acknowledgement():
    service, _ = _service()
    prepared = service.prepare("image_factory", [])
    ok = service.finish(prepared, True, "已提交打开请求；工具窗口由插件创建。")
    assert ok.ok and ok.stage == "completed"
    failed = service.finish(prepared, False, "插件未接受打开请求，请检查插件状态或重试。")
    assert failed.ok is False and failed.error_code == "LAUNCH_NOT_ACCEPTED"


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
