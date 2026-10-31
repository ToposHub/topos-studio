"""AI 凭据存储实现（TAEP §11.4）。

应用默认把 API key 保存到用户可编辑的 ``~/.topos/ai_profiles.json``，便于跨重启
保留和手动配置；``KeyringCredentialStore`` 仍提供系统钥匙串后端。profile 里只存
``credential_ref``（引用），工程文件不保存 secret，界面永不回显 token。

退化策略：macOS 上即使 Python ``keyring`` 未随打包环境提供，也通过系统 ``security``
命令访问 Keychain；其他没有安全凭据后端的平台才回退到进程内内存并记录警告。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.shared.ai_models.generation.provider_profiles import CredentialStore
from src.shared.ai_models.generation.user_ai_config import (
    USER_AI_CONFIG_PATH,
    load_user_ai_config,
    save_user_ai_config,
)

logger = logging.getLogger(__name__)

# keychain 服务名（所有 topos AI 凭据共用一个 service，username 区分各 profile）
_KEYCHAIN_SERVICE = "topos.ai-editing"

# AUD-31603：``security`` 子进程统一超时（秒）。Keychain 被其他进程持有
# 锁时，security 可能无限期阻塞；有界超时保证调用线程最多等这么久。
_SECURITY_TIMEOUT_SECONDS = 10.0

# Credential storage location preferences (stored in ai_profiles.json top-level
# ``credential_storage`` key, following the ``reasoning_effort`` precedent).
CREDENTIAL_STORAGE_FILE = "file"
CREDENTIAL_STORAGE_KEYCHAIN = "keychain"
_VALID_STORAGE_LOCATIONS = frozenset({CREDENTIAL_STORAGE_FILE, CREDENTIAL_STORAGE_KEYCHAIN})


class _MacOSSecurityKeychain:
    """通过 macOS 自带 ``security`` CLI 访问 Keychain。

    这是 Python ``keyring`` 未随打包应用带入时的安全后备路径；凭据仍然
    写入系统钥匙串，不会退化成重启即丢失的进程内存。
    """

    @staticmethod
    def _quote_arg(value: str) -> str:
        """Quote one ``security -i`` interactive-command argument.

        ``security -i`` parses its stdin lines with shell-style double
        quotes and backslash escapes (verified against the system tool);
        escaping backslashes and double quotes keeps arbitrary secrets on
        a single command line.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def get_password(service: str, username: str) -> str | None:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", username, "-w"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SECURITY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            # AUD-31603：Keychain 受锁时 get 可能超时；视同"读不到"，
            # 让上层回退到文件后端而不是无限阻塞调用线程。
            logger.warning(
                "security find-generic-password timed out (%ss) for %s",
                _SECURITY_TIMEOUT_SECONDS, service,
            )
            return None
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\r\n") or None

    @staticmethod
    def set_password(service: str, username: str, password: str) -> None:
        # Feed the write command (and the secret) to ``security -i`` over
        # stdin (AUD-31600): passing the secret as argv exposes it to every
        # local user — macOS lets any account read other users' full argv
        # via ``ps -ax -o command``.  A pipe's contents are only visible to
        # this process.  Newlines would terminate the interactive command
        # early, so they are rejected up front (they never round-tripped
        # through the argv form either).
        if "\n" in password or "\r" in password:
            raise ValueError(
                "Keychain password must not contain newline characters"
            )
        command = (
            "add-generic-password -U"
            f" -s {_MacOSSecurityKeychain._quote_arg(service)}"
            f" -a {_MacOSSecurityKeychain._quote_arg(username)}"
            f" -w {_MacOSSecurityKeychain._quote_arg(password)}\n"
        )
        try:
            result = subprocess.run(
                ["security", "-i"],
                input=command,
                capture_output=True,
                text=True,
                check=False,
                timeout=_SECURITY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            # AUD-31603：写超时必须显式失败（不能让调用方以为已保存）。
            raise RuntimeError(
                f"security add-generic-password timed out "
                f"after {_SECURITY_TIMEOUT_SECONDS}s"
            ) from exc
        if result.returncode != 0 or "returned -" in result.stdout:
            detail = result.stderr.strip() or result.stdout.strip() \
                or "unknown Keychain error"
            raise RuntimeError(detail)

    @staticmethod
    def delete_password(service: str, username: str) -> None:
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", username],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SECURITY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            # AUD-31603：删除超时只留痕——与"条目不存在"同属非致命，
            # 上层 delete_secret 本就把删除失败视为成功。
            logger.warning(
                "security delete-generic-password timed out (%ss) for %s",
                _SECURITY_TIMEOUT_SECONDS, service,
            )


class KeyringCredentialStore:
    """用 ``keyring`` 库实现 ``CredentialStore``（系统钥匙串）。

    ``credential_ref`` 作为 keychain 的 username；service 固定为
    ``topos.ai-editing``。这样多个 profile 的 key 互不冲突，且可在系统钥匙串
    管理器里按服务名批量查看/清理。
    """

    def __init__(self) -> None:
        # 延迟探测 keyring 可用性：首次访问时尝试，失败时优先使用 macOS
        # 系统 Keychain，只有没有安全后端时才降级到内存。
        self._fallback: dict[str, str] | None = None
        self._backend: Any | None = None
        self._checked = False

    def _resolve_backend(self) -> Any:
        """返回可用凭据后端；不可用时启用内存降级并返回 None。"""
        if self._checked:
            return self._backend
        self._checked = True
        try:
            import keyring  # noqa: WPS433  延迟导入，避免无后端时启动报错
            # 触发一次后端探测（get_password 对不存在的项返回 None，不报错即视为可用）
            keyring.get_password(_KEYCHAIN_SERVICE, "__topos_probe__")
            self._backend = keyring
            return self._backend
        except Exception as exc:  # noqa: BLE001  keyring 后端异常种类繁多
            if sys.platform == "darwin" and shutil.which("security"):
                logger.warning(
                    "Python keyring 不可用（%s），改用 macOS 系统 Keychain", exc
                )
                self._backend = _MacOSSecurityKeychain()
                return self._backend
            logger.warning(
                "keyring 不可用（%s），AI 凭据回退到内存存储（重启后失效）", exc
            )
            self._fallback = {}
            return None

    # ---------------- CredentialStore 接口 ---------------- #

    def get_secret(self, credential_ref: str) -> str | None:
        backend = self._resolve_backend()
        if backend is None:
            assert self._fallback is not None
            return self._fallback.get(credential_ref)
        try:
            return backend.get_password(_KEYCHAIN_SERVICE, credential_ref)
        except Exception as exc:  # noqa: BLE001
            logger.debug("keyring get_secret 失败：%s", exc)
            return None

    def set_secret(self, credential_ref: str, secret: str) -> None:
        backend = self._resolve_backend()
        if backend is None:
            assert self._fallback is not None
            self._fallback[credential_ref] = secret
            return
        try:
            backend.set_password(_KEYCHAIN_SERVICE, credential_ref, secret)
        except Exception as exc:  # noqa: BLE001
            logger.error("保存 API key 到钥匙串失败：%s", exc)
            raise

    def delete_secret(self, credential_ref: str) -> None:
        backend = self._resolve_backend()
        if backend is None:
            assert self._fallback is not None
            self._fallback.pop(credential_ref, None)
            return
        try:
            backend.delete_password(_KEYCHAIN_SERVICE, credential_ref)
        except Exception:  # noqa: BLE001  删除不存在的项视为成功
            pass

    @property
    def durable_backend_available(self) -> bool:
        """Return whether this instance is backed by a persistent secure store."""

        return self._resolve_backend() is not None


class HybridCredentialStore:
    """A credential store that reads from both file and keychain.

    The ``prefer`` parameter controls **where new secrets are written** and
    **which backend is checked first on read**:

    - ``prefer="keychain"`` (legacy default): write to keychain, read
      keychain-first.  Use when the user opts into the more secure backend.
    - ``prefer="file"`` (new default): write to ``~/.topos/ai_profiles.json``,
      read file-first with keychain fallback.  Use by default so the config
      file is self-contained and portable across machines.

    Both modes read from **both** locations so switching the preference never
    loses access to legacy keys still stored in the other backend.  New
    writes, however, update only the preferred backend and remove the other
    backend's copy of the same ref (see :meth:`set_secret`) so a rotated-out
    key can never shadow the fresh one after a preference switch.
    """

    def __init__(
        self,
        secure_store: KeyringCredentialStore | None = None,
        file_store: FileCredentialStore | None = None,
        *,
        prefer: str = CREDENTIAL_STORAGE_FILE,
    ) -> None:
        self._secure_store = secure_store or KeyringCredentialStore()
        self._file_store = file_store or FileCredentialStore()
        self._prefer = prefer if prefer in _VALID_STORAGE_LOCATIONS else CREDENTIAL_STORAGE_FILE

    def get_secret(self, credential_ref: str) -> str | None:
        if self._prefer == CREDENTIAL_STORAGE_FILE:
            secret = self._file_store.get_secret(credential_ref)
            return secret or self._secure_store.get_secret(credential_ref)
        secret = self._secure_store.get_secret(credential_ref)
        return secret or self._file_store.get_secret(credential_ref)

    def set_secret(self, credential_ref: str, secret: str) -> None:
        """Write the secret to the preferred backend ONLY (AUD-31601).

        The other backend's copy for the same ref is deleted after the
        write succeeds: previously ``prefer="file"`` left the previous
        (possibly rotated-out/revoked) key in the keychain, which then
        shadowed the fresh value as soon as the user switched
        ``prefer`` back to ``keychain`` — authentication kept silently
        using stale credentials.  Keeping exactly one authoritative copy
        makes reads under either preference return the newest key.
        """
        if self._prefer == CREDENTIAL_STORAGE_FILE:
            self._file_store.set_secret(credential_ref, secret)
            self._secure_store.delete_secret(credential_ref)
            return
        self._secure_store.set_secret(credential_ref, secret)
        if not self._secure_store.durable_backend_available:
            # Memory-fallback keychain store loses everything on restart;
            # keep the durable file copy as the surviving record.
            self._file_store.set_secret(credential_ref, secret)
        else:
            self._file_store.delete_secret(credential_ref)

    def delete_secret(self, credential_ref: str) -> None:
        self._secure_store.delete_secret(credential_ref)
        self._file_store.delete_secret(credential_ref)


class FileCredentialStore:
    """把凭据保存到用户可编辑的 ``~/.topos/ai_profiles.json``。

    这是应用默认使用的简单后端。文件权限会收紧为 0600，但 JSON 中的 key
    仍是明文，用户应自行保护 home 目录；系统钥匙串后端仍由
    :class:`KeyringCredentialStore` 保留，供需要更高安全性的环境使用。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or USER_AI_CONFIG_PATH

    def get_secret(self, credential_ref: str) -> str | None:
        config = load_user_ai_config(self._path)
        credentials = config.get("credentials")
        if not isinstance(credentials, dict):
            return None
        secret = credentials.get(credential_ref)
        return secret if isinstance(secret, str) and secret else None

    def set_secret(self, credential_ref: str, secret: str) -> None:
        config = load_user_ai_config(self._path)
        credentials = config.get("credentials")
        if not isinstance(credentials, dict):
            credentials = {}
        credentials = dict(credentials)
        credentials[credential_ref] = secret
        config["credentials"] = credentials
        save_user_ai_config(config, self._path)

    def delete_secret(self, credential_ref: str) -> None:
        config = load_user_ai_config(self._path)
        credentials = config.get("credentials")
        if not isinstance(credentials, dict) or credential_ref not in credentials:
            return
        credentials = dict(credentials)
        credentials.pop(credential_ref, None)
        config["credentials"] = credentials
        save_user_ai_config(config, self._path)


def make_credential_ref(profile_id: str) -> str:
    """为 profile 生成稳定的 credential_ref（用于凭据文件或 keychain）。

    格式 ``topos:{profile_id}``，稳定可追溯。
    """
    return f"topos:{profile_id}"


# ---------------------------------------------------------------------- #
# Storage-location preference + factory
# ---------------------------------------------------------------------- #


def get_credential_storage_preference() -> str:
    """Read the user's credential storage preference from ``ai_profiles.json``.

    Returns ``"file"`` (default — portable, self-contained config) or
    ``"keychain"`` (opt-in — more secure, not portable across machines).
    """

    location = load_user_ai_config().get("credential_storage", CREDENTIAL_STORAGE_FILE)
    return location if location in _VALID_STORAGE_LOCATIONS else CREDENTIAL_STORAGE_FILE


def set_credential_storage_preference(location: str) -> None:
    """Persist the credential storage preference to ``ai_profiles.json``."""

    if location not in _VALID_STORAGE_LOCATIONS:
        raise ValueError(f"invalid credential storage location: {location!r}")
    config = load_user_ai_config()
    config["credential_storage"] = location
    save_user_ai_config(config)


def make_credential_store(location: str = CREDENTIAL_STORAGE_FILE) -> CredentialStore:
    """Build a credential store for the given storage ``location``.

    - ``"file"`` (default): writes to ``~/.topos/ai_profiles.json``, reads
      file-first with keychain fallback (portable + backward compatible).
    - ``"keychain"``: writes to the system keychain, reads keychain-first
      (more secure, not portable).

    Both modes read from both backends so existing keys are never lost when
    switching the preference.
    """

    if location == CREDENTIAL_STORAGE_KEYCHAIN:
        return HybridCredentialStore(prefer=CREDENTIAL_STORAGE_KEYCHAIN)
    return HybridCredentialStore(prefer=CREDENTIAL_STORAGE_FILE)


def migrate_credentials_to_file(
    credential_refs: "Any",
) -> int:
    """Copy keychain-only secrets into ``ai_profiles.json`` (one-time migration).

    Iterates the given ``credential_refs`` (an iterable of ref strings or
    objects with a ``credential_ref`` attribute).  For each ref that exists in
    the keychain but NOT in the file, copies it to the file so the config
    becomes self-contained and portable.

    Returns the number of secrets migrated.  Existing file secrets are never
    overwritten.
    """

    file_store = FileCredentialStore()
    keychain_store = KeyringCredentialStore()
    migrated = 0
    for item in credential_refs:
        ref = item.credential_ref if hasattr(item, "credential_ref") else item
        if not isinstance(ref, str) or not ref:
            continue
        if file_store.get_secret(ref):
            continue  # already in file, skip
        secret = keychain_store.get_secret(ref)
        if secret:
            file_store.set_secret(ref, secret)
            migrated += 1
    return migrated


# 鸭子类型断言：KeyringCredentialStore 满足 CredentialStore Protocol
#（Protocol 是结构化类型，无需显式继承；此行仅用于静态检查器与文档可读性）
_ASSERT_PROTOCOL: CredentialStore = KeyringCredentialStore()  # type: ignore[assignment]
_ASSERT_FILE_PROTOCOL: CredentialStore = FileCredentialStore()  # type: ignore[assignment]
_ASSERT_HYBRID_PROTOCOL: CredentialStore = HybridCredentialStore()  # type: ignore[assignment]
