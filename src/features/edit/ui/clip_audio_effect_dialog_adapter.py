"""P-clip-FX: clip-level audio effect dialog adapter.

复用 Audio Mixer 的 8 个独立参数对话框（StripEQDialog / ReverbDialog / ...），
让它们也能为**单个音频片段**上的 mixer 效果打开。

Mixer dialog 是为 track-level 设计的：
  - 构造签名 ``(track_id, track_name, instance_id, initial_params, parent)``
    （StripEQDialog 无 instance_id）
  - 信号 ``<type>_committed(track_id, *Params dataclass)``
  - 内部状态用强类型 ``*Params`` dataclass

Clip-level 场景则用 dict + (clip_id, effect_id)。本适配层负责两种表示的互转：
  - 打开时：clip 的 ``EffectInstance.params`` (dict) → ``*Params.from_dict()`` 喂给 dialog
  - 提交时：dialog emit 的 ``*Params`` → ``.to_dict()`` → 通过
    ``clip_effect_params_committed`` 信号交由 controller 落库（MacroCommand 聚合，
    单步撤销）

Dialog 按 ``(clip_id, effect_id)`` 缓存，重复点击「打开面板」复用已打开的窗口
（沿用 mixer 的 ``_xxx_dialogs`` dict 模式）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: F401（占位对话框用）
from PyQt6.QtWidgets import QDialog, QWidget

logger = logging.getLogger(__name__)


# effect_type → (DialogClass, ParamsClass, committed_signal_attr, preview_signal_attr)
# 延迟构建避免循环 import（dialog 模块在 import 时拉起大量 UI 依赖）。
_DIALOG_REGISTRY: Optional[dict] = None


class _NFPlaceholderDialog(QDialog):
    """NF 两款新 FX 的过渡参数对话框（NF-02 换正式窗口）。

    通用键值编辑器：列出 params dict 的键，可编辑数值/布尔/字符串，
    提交时整体回发 committed 信号。满足 clip 级试听/提交契约，不冒充
    完整 UI（正式版见 NF-02 voice_changer_dialog/spectral_mangler_dialog）。
    """

    nf_committed = pyqtSignal(str, object)

    def __init__(self, track_id, track_name, instance_id, initial_params=None,
                 parent=None, **kwargs):
        super().__init__(parent)
        from PyQt6.QtWidgets import (
            QDialogButtonBox, QFormLayout, QLineEdit,
        )
        self._track_id = track_id
        self._instance_id = instance_id
        self.value_committed = None
        self.setWindowTitle(f"{track_name} · FX")
        form = QFormLayout(self)
        self._edits: dict = {}
        params = dict(initial_params or {})
        for key in sorted(params):
            edit = QLineEdit(str(params[key]))
            form.addRow(str(key), edit)
            self._edits[key] = edit
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(self._commit)
        buttons.rejected.connect(self.reject)

    def _commit(self):
        params = {}
        for key, edit in self._edits.items():
            text = edit.text().strip()
            try:
                params[key] = float(text) if ("." in text or "e" in text.lower()) else (
                    int(text) if text.lstrip("-").isdigit()
                    else (True if text.lower() == "true"
                          else (False if text.lower() == "false" else text)))
            except ValueError:
                params[key] = text
        self.nf_committed.emit(self._track_id, params)
        self.accept()


def _ensure_dialog_registry() -> dict:
    """惰性构建 effect_type → dialog 元数据映射。"""
    global _DIALOG_REGISTRY
    if _DIALOG_REGISTRY is not None:
        return _DIALOG_REGISTRY

    from src.features.edit.core.timeline_v2 import (
        StripEQParams, LimiterParams, CompressorParams, ReverbParams,
        DelayParams, SaturationParams, ImagerParams, ExpanderParams,
        DynamicsParams, VoiceChangerParams, SpectralManglerParams,
    )
    from src.shared.ui.audio_mixer.strip_eq_dialog import ParametricEQDialog
    from src.shared.ui.audio_mixer.limiter_dialog import LimiterDialog
    from src.shared.ui.audio_mixer.compressor_dialog import CompressorDialog
    from src.shared.ui.audio_mixer.reverb_dialog import ReverbDialog
    from src.shared.ui.audio_mixer.delay_dialog import DelayDialog
    from src.shared.ui.audio_mixer.saturation_dialog import SaturationDialog
    from src.shared.ui.audio_mixer.imager_dialog import ImagerDialog
    from src.shared.ui.audio_mixer.expander_dialog import ExpanderDialog
    from src.shared.ui.audio_mixer.dynamics_dialog import ClipDynamicsDialog

    # 每项: (DialogClass, ParamsClass, committed_signal_name, has_instance_id)
    # CW-A1: 全部对话框统一带 instance_id 构造；expander/noise_gate 共用
    # ExpanderDialog（旧 gate 参数经 ExpanderParams.from_dict 无损转 Gate）。
    _DIALOG_REGISTRY = {
        "strip_eq":    (ParametricEQDialog, StripEQParams,   "strip_eq_committed",   True),
        "limiter":     (LimiterDialog,    LimiterParams,    "limiter_committed",     True),
        "compressor":  (CompressorDialog, CompressorParams, "compressor_committed",  True),
        "reverb":      (ReverbDialog,     ReverbParams,     "reverb_committed",      True),
        "delay":       (DelayDialog,      DelayParams,      "delay_committed",       True),
        "saturation":  (SaturationDialog, SaturationParams, "saturation_committed",  True),
        "imager":      (ImagerDialog,     ImagerParams,     "imager_committed",      True),
        "expander":    (ExpanderDialog,   ExpanderParams,   "expander_committed",    True),
        "noise_gate":  (ExpanderDialog,   ExpanderParams,   "expander_committed",    True),
        # CW-A1: Dynamics 完整单元（EG→Comp→Lim 单实例复合，不拆分）
        "dynamics":    (ClipDynamicsDialog, DynamicsParams, "dynamics_committed",    True),
        # NF（2026-11-15 计划 §6.3）：两款新复合 FX——NF-02 批次接入
        # 完整窗口；当前注册轻量参数对话框占位（可打开/提交，样式后补）。
        "voice_changer": (_NFPlaceholderDialog, VoiceChangerParams,
                          "nf_committed", True),
        "spectral_mangler": (_NFPlaceholderDialog, SpectralManglerParams,
                             "nf_committed", True),
    }
    return _DIALOG_REGISTRY


class ClipAudioEffectDialogManager(QObject):
    """管理 clip-level audio 效果独立对话框的创建 / 缓存 / 信号转发。

    Usage:
        manager = ClipAudioEffectDialogManager(parent_panel)
        manager.clip_effect_params_committed.connect(controller_handler)
        manager.open_dialog(effect_type, clip_id, effect_id, params_dict, clip_name)
    """

    # 提交信号：(clip_id, effect_id, full_params_dict)
    # controller 收到后用 MacroCommand 把变化字段逐个 UpdateEffectParamCommand
    # 聚合成单步撤销。
    clip_effect_params_committed = pyqtSignal(str, str, dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # (clip_id, effect_id) → QDialog 实例（复用，避免重复弹出）
        self._dialogs: dict[tuple[str, str], QDialog] = {}

    def open_dialog(
        self,
        effect_type: str,
        clip_id: str,
        effect_id: str,
        params_dict: dict[str, Any],
        clip_name: str = "",
        parent: Optional[QWidget] = None,
    ) -> Optional[QDialog]:
        """打开（或聚焦）指定 clip 效果的独立参数对话框。

        Args:
            effect_type: mixer audio effect_type（strip_eq/reverb/...）
            clip_id: 片段 ID
            effect_id: 效果实例 ID
            params_dict: 当前 EffectInstance.params（完整 dict）
            clip_name: 用于窗口标题的片段名
            parent: 父窗口（None 时用 manager 的 parent）

        Returns:
            打开的 QDialog，effect_type 不支持时返回 None。
        """
        registry = _ensure_dialog_registry()
        meta = registry.get(effect_type)
        if meta is None:
            return None  # 非 mixer audio 效果，不弹窗
        dialog_cls, params_cls, committed_attr, has_instance_id = meta

        key = (clip_id, effect_id)
        cached = self._dialogs.get(key)
        if cached is not None:
            # 复用已打开的窗口：聚焦并返回（不重建，保留用户调整中的状态）
            if cached.isVisible():
                cached.raise_()
                cached.activateWindow()
                return cached
            # 窗口已被关闭但对象还在 → 清理后重建
            self._dialogs.pop(key, None)

        # dict → Params dataclass（from_dict 对缺失字段用默认值，容错）
        try:
            initial_params = params_cls.from_dict(dict(params_dict))
        except Exception:  # noqa: BLE001 — 防御：用默认参数兜底
            # AUD-19720: 持久化参数损坏（旧版本数据/手改导致类型错误或
            # 超出 __post_init__ 校验范围）时降级为默认参数打开对话框。
            # 降级必须留下日志：否则用户点确定会把默认值整包提交，
            # 真实效果参数被静默覆盖且无从排查。
            logger.warning(
                "Clip audio effect params failed to parse (effect_type=%s, "
                "clip_id=%s, effect_id=%s) — opening dialog with DEFAULT "
                "params; committing them would overwrite the stored params",
                effect_type, clip_id, effect_id,
                exc_info=True,
            )
            initial_params = params_cls.default()

        # 构造 dialog。track_id/track_name 槽位借用 clip_id/clip_name 仅用于
        # 窗口标题与信号 payload 的占位（dialog 内部不做 track 级 DSP）。
        # CW-A1: 全部对话框统一带 instance_id 构造。
        win_parent = parent if parent is not None else self.parent()
        dialog = dialog_cls(
            track_id=clip_id,
            track_name=clip_name or clip_id,
            instance_id=effect_id,
            initial_params=initial_params,
            parent=win_parent,
        )

        # 连接 committed 信号 → 转 dict → emit clip_effect_params_committed
        committed_signal = getattr(dialog, committed_attr, None)
        if committed_signal is not None:
            committed_signal.connect(
                lambda _tid, params, cid=clip_id, eid=effect_id:
                    self._on_committed(cid, eid, params)
            )

        # 窗口关闭时清理缓存引用（避免持有已销毁的 C++ 对象）
        dialog.finished.connect(
            lambda _result, k=key: self._dialogs.pop(k, None)
        )
        # 关闭即确定性销毁 C++ 对象。否则无父窗口的顶层 dialog 只被
        # Python 引用持有，已关闭对象滞留到 GC 任意时刻才析构——
        # ~QDialog→deleteChildren→~QComboBox→clearFocus 会向此时已
        # 失效的焦点链发事件（lldb 实测 EXC_BAD_ACCESS，随后的测试
        # 模块 import 期段错误）。调用方均不保留返回值，安全。
        from PyQt6.QtCore import Qt
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._dialogs[key] = dialog
        dialog.show()
        return dialog

    def _on_committed(self, clip_id: str, effect_id: str, params: Any) -> None:
        """dialog committed 信号 → 转成 dict → emit 给 controller 落库。

        Args:
            clip_id: 片段 ID
            effect_id: 效果实例 ID
            params: mixer ``*Params`` dataclass（dialog 回传）
        """
        try:
            params_dict = params.to_dict() if params is not None else {}
        except Exception:  # noqa: BLE001
            return
        if not params_dict:
            return
        self.clip_effect_params_committed.emit(clip_id, effect_id, dict(params_dict))

    def close_all(self) -> None:
        """关闭并清理所有已打开的对话框（面板销毁时调用）。"""
        for dialog in list(self._dialogs.values()):
            try:
                dialog.close()
            except RuntimeError:
                pass  # 对象可能已被销毁
        self._dialogs.clear()
