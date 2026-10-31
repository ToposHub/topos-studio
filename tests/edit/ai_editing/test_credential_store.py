"""CredentialStore 后端选择与 macOS Keychain 后备路径测试。"""

from __future__ import annotations

from types import SimpleNamespace

from src.features.edit.ai_editing import credential_store as module


def test_keychain_backend_is_cached(monkeypatch):
    """探测一次后应继续使用同一个 keyring 模块，而不是字符串哨兵。"""
    calls = []

    class FakeKeyring:
        @staticmethod
        def get_password(service, username):
            calls.append((service, username))
            return None

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)
    store = module.KeyringCredentialStore()

    backend = store._resolve_backend()
    assert backend is FakeKeyring
    assert store._resolve_backend() is FakeKeyring
    assert len(calls) == 1


def test_macos_security_backend_uses_keychain_commands(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == "find-generic-password":
            return SimpleNamespace(returncode=0, stdout="saved-key\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    backend = module._MacOSSecurityKeychain()

    assert backend.get_password("svc", "user") == "saved-key"
    backend.set_password("svc", "user", "new-key")
    backend.delete_password("svc", "user")

    assert calls[0][0] == [
        "security", "find-generic-password", "-s", "svc", "-a", "user", "-w",
    ]
    # AUD-31600: the secret is fed to ``security -i`` over stdin (quoted),
    # never as argv where other local users could see it via ps.
    assert calls[1][0] == ["security", "-i"]
    assert calls[1][1]["input"] == (
        'add-generic-password -U -s "svc" -a "user" -w "new-key"\n'
    )
    assert "new-key" not in calls[1][0]
    assert calls[2][0] == [
        "security", "delete-generic-password", "-s", "svc", "-a", "user",
    ]


def test_hybrid_store_keychain_mode_reads_keychain_first(monkeypatch, tmp_path):
    """prefer="keychain": keychain secret wins over file on read."""
    secure = module.KeyringCredentialStore()
    file_store = module.FileCredentialStore(tmp_path / "ai_profiles.json")
    file_store.set_secret("topos:demo", "legacy-secret")
    monkeypatch.setattr(secure, "get_secret", lambda ref: "keychain-secret")
    hybrid = module.HybridCredentialStore(secure, file_store, prefer="keychain")

    assert hybrid.get_secret("topos:demo") == "keychain-secret"


def test_hybrid_store_file_mode_reads_file_first(monkeypatch, tmp_path):
    """prefer="file" (default): file secret wins over keychain on read."""
    secure = module.KeyringCredentialStore()
    file_store = module.FileCredentialStore(tmp_path / "ai_profiles.json")
    file_store.set_secret("topos:demo", "file-secret")
    monkeypatch.setattr(secure, "get_secret", lambda ref: "keychain-secret")
    hybrid = module.HybridCredentialStore(secure, file_store, prefer="file")

    assert hybrid.get_secret("topos:demo") == "file-secret"


def test_hybrid_store_file_mode_falls_back_to_keychain(monkeypatch, tmp_path):
    """prefer="file": if the file has no key, the keychain is still checked."""
    secure = module.KeyringCredentialStore()
    file_store = module.FileCredentialStore(tmp_path / "ai_profiles.json")
    monkeypatch.setattr(secure, "get_secret", lambda ref: "keychain-only-secret")
    hybrid = module.HybridCredentialStore(secure, file_store, prefer="file")

    assert hybrid.get_secret("topos:demo") == "keychain-only-secret"


def test_hybrid_store_file_mode_writes_to_file_only(monkeypatch, tmp_path):
    """prefer="file": set_secret writes to file, NOT keychain."""
    written_to_keychain = []

    class FakeSecure:
        durable_backend_available = True

        def get_secret(self, ref):
            return None

        def set_secret(self, ref, secret):
            written_to_keychain.append((ref, secret))

        def delete_secret(self, ref):
            return None

    file_store = module.FileCredentialStore(tmp_path / "ai_profiles.json")
    hybrid = module.HybridCredentialStore(FakeSecure(), file_store, prefer="file")

    hybrid.set_secret("topos:demo", "my-key")

    assert file_store.get_secret("topos:demo") == "my-key"
    assert written_to_keychain == []  # keychain was NOT written


def test_hybrid_store_keychain_mode_falls_back_to_file_when_not_durable(
    monkeypatch, tmp_path,
):
    class NonDurableStore:
        durable_backend_available = False

        def get_secret(self, ref):
            return None

        def set_secret(self, ref, secret):
            return None

        def delete_secret(self, ref):
            return None

    secure = NonDurableStore()
    file_store = module.FileCredentialStore(tmp_path / "ai_profiles.json")
    hybrid = module.HybridCredentialStore(secure, file_store, prefer="keychain")

    hybrid.set_secret("topos:demo", "legacy-secret")

    assert file_store.get_secret("topos:demo") == "legacy-secret"


# ---------------------------------------------------------------------- #
# Factory + preference + migration
# ---------------------------------------------------------------------- #


def test_make_credential_store_file_writes_to_file(tmp_path):
    """Factory with "file" produces a store that writes to the config file."""
    monkeypatch_target = "src.features.edit.ai_editing.credential_store.USER_AI_CONFIG_PATH"
    import pytest

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(monkeypatch_target, tmp_path / "ai_profiles.json")
        store = module.make_credential_store("file")
        store.set_secret("topos:test", "secret-value")
        file_store = module.FileCredentialStore(tmp_path / "ai_profiles.json")
        assert file_store.get_secret("topos:test") == "secret-value"


def test_credential_storage_preference_round_trip(monkeypatch, tmp_path):
    """Preference is persisted to ai_profiles.json and survives reload.

    AUD-R2-007: the get/set helpers resolve their default path inside the
    ``user_ai_config`` module (not via ``credential_store``'s imported
    binding), so patching only ``module.USER_AI_CONFIG_PATH`` left the test
    reading — and worse, writing — the host machine's real
    ``~/.topos/ai_profiles.json``, and it failed outright when the host
    preference was "keychain". Patch the preference source in
    ``user_ai_config`` itself (plus the credential_store copy) so the test
    always starts from a pristine config with the known "file" default.
    """
    from src.features.edit.ai_editing import user_ai_config

    config_path = tmp_path / "ai_profiles.json"
    monkeypatch.setattr(user_ai_config, "USER_AI_CONFIG_PATH", config_path)
    monkeypatch.setattr(module, "USER_AI_CONFIG_PATH", config_path)
    from src.features.edit.ai_editing.user_ai_config import (
        load_user_ai_config, save_user_ai_config,
    )
    save_user_ai_config({}, config_path)

    assert module.get_credential_storage_preference() == "file"  # default

    module.set_credential_storage_preference("keychain")
    assert module.get_credential_storage_preference() == "keychain"
    assert load_user_ai_config(config_path).get("credential_storage") == "keychain"

    module.set_credential_storage_preference("file")
    assert module.get_credential_storage_preference() == "file"
    assert load_user_ai_config(config_path).get("credential_storage") == "file"


def test_migrate_credentials_to_file_copies_keychain_only_secrets(monkeypatch, tmp_path):
    """Migration copies secrets that exist in keychain but not in file."""
    config_path = tmp_path / "ai_profiles.json"
    monkeypatch.setattr(module, "USER_AI_CONFIG_PATH", config_path)
    from src.features.edit.ai_editing.user_ai_config import save_user_ai_config
    save_user_ai_config({}, config_path)

    secure = module.KeyringCredentialStore()
    monkeypatch.setattr(secure, "get_secret", lambda ref: "from-keychain" if ref == "topos:kc" else None)

    refs = [SimpleNamespace(credential_ref="topos:kc"),
            SimpleNamespace(credential_ref="topos:already-in-file")]
    # Pre-seed one ref in the file so it's skipped
    file_store = module.FileCredentialStore(config_path)
    file_store.set_secret("topos:already-in-file", "existing")

    migrated = module.migrate_credentials_to_file.__wrapped__ if hasattr(module.migrate_credentials_to_file, '__wrapped__') else module.migrate_credentials_to_file
    # Call with explicit stores would be ideal, but the function creates its own.
    # Monkeypatch the module-level stores instead.
    monkeypatch.setattr(module, "FileCredentialStore", lambda path=None: file_store)
    monkeypatch.setattr(module, "KeyringCredentialStore", lambda: secure)

    count = module.migrate_credentials_to_file(refs)

    assert count == 1
    assert file_store.get_secret("topos:kc") == "from-keychain"
    assert file_store.get_secret("topos:already-in-file") == "existing"
