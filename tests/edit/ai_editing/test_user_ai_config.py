"""用户级 ``~/.topos`` AI 配置与凭据持久化测试。"""

from __future__ import annotations

import json
import os
import stat

from src.features.edit.ai_editing.credential_store import FileCredentialStore
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    ProfileRegistry,
)
from src.features.edit.ai_editing.user_ai_config import (
    load_user_ai_config,
    save_user_ai_config,
)


def test_user_config_round_trip_and_restrictive_permissions(tmp_path):
    path = tmp_path / ".topos" / "ai_profiles.json"
    save_user_ai_config({"active_profile_id": "demo", "profiles": []}, path)

    assert load_user_ai_config(path)["active_profile_id"] == "demo"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        # W07：Windows 无 POSIX 权限位（chmod 只切只读位）——改验产品
        # 确实按 0o600 调用过 chmod（意图留痕），往返内容断言不变。
        import unittest.mock as _mock

        real_chmod = os.chmod
        calls = []

        def _spy(p_, *a, **k):
            if str(path) in str(p_):
                calls.append(a[0] if a else k.get("mode"))
            return real_chmod(p_, *a, **k)

        with _mock.patch("os.chmod", _spy):
            save_user_ai_config(
                {"active_profile_id": "demo", "profiles": []}, path)
        assert 0o600 in calls
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["profiles"] == []


def test_file_credential_store_persists_secret(tmp_path):
    path = tmp_path / ".topos" / "ai_profiles.json"
    store = FileCredentialStore(path)
    store.set_secret("topos:demo", "secret-value")

    assert FileCredentialStore(path).get_secret("topos:demo") == "secret-value"
    store.delete_secret("topos:demo")
    assert FileCredentialStore(path).get_secret("topos:demo") is None


def test_profile_registry_round_trips_through_user_file(tmp_path):
    path = tmp_path / ".topos" / "ai_profiles.json"
    registry = ProfileRegistry()
    registry.add(
        AiProviderProfile(
            profile_id="demo",
            display_name="Demo",
            endpoint="http://localhost:11434",
            model_id="llama3.2",
            credential_ref="topos:demo",
        )
    )
    registry.set_active("demo")
    registry.save_to_user_file(path)

    restored = ProfileRegistry()
    assert restored.load_from_user_file(path)
    assert restored.active_profile_id == "demo"
    assert restored.get("demo").model_id == "llama3.2"
