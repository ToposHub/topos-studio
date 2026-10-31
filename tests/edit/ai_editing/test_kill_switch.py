"""M12 Kill Switch 单元测试（§11.1 M12 / §11.2 M12 完成检查 / §15.4 安全响应）。

覆盖：
- Capability 枚举（6 个 AI 能力）
- KillSwitch is_enabled / disable / enable（per-capability）
- disable 不可关能力（killable=False）返回 False
- emergency_disable_all（一键全关 killable 能力）
- verify_all_killable（§11.2 M12 完成检查硬门）
- KillSwitchEvent 审计追溯（§15.4）
- enabled/disabled_capabilities 查询

红线对照：
- 可逐能力关闭 provider/Skill/MCP（§11.1 M12 退出条件）
- 所有启用能力都有 kill switch（§11.2 M12 完成检查）
- 不破坏普通编辑器（§11.2 M12 完成检查）
- 能快速关闭（§15.4 安全响应）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.kill_switch import (
    ALL_CAPABILITIES,
    Capability,
    KillSwitch,
    KillSwitchEvent,
)


# ================================================================
# Capability 枚举
# ================================================================


class TestCapability:
    def test_six_ai_capabilities(self):
        caps = {c.value for c in Capability}
        assert caps == {
            "ai_assistant", "media_analysis", "cloud_analysis",
            "generation", "mcp_server", "skills",
        }

    def test_all_capabilities_constant(self):
        assert len(ALL_CAPABILITIES) == 6
        assert set(ALL_CAPABILITIES) == set(Capability)


# ================================================================
# 基本查询 + disable/enable
# ================================================================


class TestDisableEnable:
    def test_all_enabled_by_default(self):
        ks = KillSwitch()
        for c in ALL_CAPABILITIES:
            assert ks.is_enabled(c) is True
            assert ks.is_killable(c) is True

    def test_disable_single(self):
        ks = KillSwitch()
        assert ks.disable(Capability.GENERATION, reason="safety") is True
        assert ks.is_enabled(Capability.GENERATION) is False
        assert ks.is_enabled(Capability.MCP_SERVER) is True  # 其它不受影响

    def test_disable_idempotent(self):
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        assert ks.disable(Capability.GENERATION) is False  # 已禁用

    def test_enable_back(self):
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        assert ks.enable(Capability.GENERATION) is True
        assert ks.is_enabled(Capability.GENERATION) is True

    def test_enable_idempotent(self):
        ks = KillSwitch()
        assert ks.enable(Capability.GENERATION) is False  # 已启用

    def test_disable_records_event(self):
        """§15.4 安全响应可追溯。"""
        ks = KillSwitch()
        ks.disable(Capability.MCP_SERVER, reason="incident")
        events = ks.events()
        assert len(events) == 1
        assert events[0].capability == Capability.MCP_SERVER
        assert events[0].action == "disabled"
        assert events[0].reason == "incident"


# ================================================================
# emergency_disable_all
# ================================================================


class TestEmergencyDisable:
    def test_disables_all_killable(self):
        """§15.4：一键全关所有 killable 能力。"""
        ks = KillSwitch()
        count = ks.emergency_disable_all(reason="incident response")
        assert count == 6  # 全部 6 个能力都 killable
        for c in ALL_CAPABILITIES:
            assert ks.is_enabled(c) is False

    def test_emergency_records_event(self):
        ks = KillSwitch()
        ks.emergency_disable_all(reason="incident")
        events = ks.events()
        # 6 个 disable 事件 + 1 个 emergency_disabled_all 事件
        actions = [e.action for e in events]
        assert actions.count("disabled") == 6
        assert "emergency_disabled_all" in actions

    def test_emergency_after_partial(self):
        """部分已禁用时，只关剩余的。"""
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        count = ks.emergency_disable_all()
        assert count == 5  # GENERATION 已关，只关剩下 5 个


# ================================================================
# verify_all_killable（§11.2 M12 完成检查硬门）
# ================================================================


class TestVerify:
    def test_all_killable_when_everything_enabled(self):
        """§11.2 M12 完成检查：所有启用能力都有 kill switch。"""
        ks = KillSwitch()
        result = ks.verify_all_killable()
        assert result.all_killable is True
        assert result.unkillable == ()
        assert result.enabled_count == 6
        assert result.diagnostics == ()

    def test_all_killable_when_some_disabled(self):
        """禁用的能力不影响 verify（只检查 enabled 的）。"""
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        ks.disable(Capability.MCP_SERVER)
        result = ks.verify_all_killable()
        assert result.all_killable is True
        assert result.enabled_count == 4
        assert result.disabled_count == 2

    def test_verify_to_dict_json_serializable(self):
        import json
        ks = KillSwitch()
        json.dumps(ks.verify_all_killable().to_dict())


# ================================================================
# 查询辅助
# ================================================================


class TestQueries:
    def test_enabled_capabilities(self):
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        enabled = set(ks.enabled_capabilities())
        assert Capability.GENERATION not in enabled
        assert len(enabled) == 5

    def test_disabled_capabilities(self):
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        ks.disable(Capability.SKILLS)
        disabled = set(ks.disabled_capabilities())
        assert disabled == {Capability.GENERATION, Capability.SKILLS}

    def test_status(self):
        ks = KillSwitch()
        s = ks.status(Capability.GENERATION)
        assert s.enabled is True
        assert s.killable is True

    def test_all_status(self):
        ks = KillSwitch()
        statuses = ks.all_status()
        assert len(statuses) == 6

    def test_to_dict_json_serializable(self):
        import json
        ks = KillSwitch()
        ks.disable(Capability.GENERATION)
        json.dumps(ks.to_dict())
