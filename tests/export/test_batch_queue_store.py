"""P5-T6：batch_queue_store 专测。

批量导出队列持久化存储的公共 API 覆盖：
- BatchQueueItem（to_dict/from_dict/is_restorable）
- save_batch_queue / load_batch_queue / clear_batch_queue /
  has_restorable_items

写盘全部经 TOPOS_CONFIG_PATH 重定向到 tmp_path，不触碰真实用户
配置目录；失败路径用 /dev/null（字符设备，非目录）制造 makedirs
失败。AUD-33115 的"settings=None 保旧设置"语义为回归重点。
"""

from __future__ import annotations

import json
import os

import pytest

from src.shared.export.batch_queue_store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RENDERING,
    BatchQueueItem,
    clear_batch_queue,
    has_restorable_items,
    load_batch_queue,
    save_batch_queue,
)


@pytest.fixture
def queue_env(tmp_path, monkeypatch):
    """把队列文件重定向到 tmp_path/batch_queue.json。"""
    monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
    return tmp_path / "batch_queue.json"


def _make_items() -> list[BatchQueueItem]:
    return [
        BatchQueueItem(file_path="/media/a.mp4", status=STATUS_DONE,
                       output_path="/out/a_topos.mp4", started_at=1.0,
                       finished_at=2.0),
        BatchQueueItem(file_path="/media/b.mp4", status=STATUS_FAILED,
                       error="boom"),
        BatchQueueItem(file_path="/media/c.mp4"),  # 默认 pending
    ]


class TestBatchQueueItem:
    def test_to_dict_from_dict_roundtrip(self) -> None:
        item = BatchQueueItem(file_path="/x/y.mp4", status=STATUS_RENDERING,
                              output_path="/o.mp4", error=None,
                              started_at=10.5, finished_at=None)
        restored = BatchQueueItem.from_dict(item.to_dict())
        assert restored == item

    def test_defaults_are_pending(self) -> None:
        item = BatchQueueItem(file_path="/x.mp4")
        assert item.status == STATUS_PENDING
        assert item.output_path is None
        assert item.error is None
        assert item.started_at is None
        assert item.finished_at is None

    def test_from_dict_missing_fields_uses_defaults(self) -> None:
        item = BatchQueueItem.from_dict({})
        assert item.file_path == ""
        assert item.status == STATUS_PENDING

    @pytest.mark.parametrize("status,expected", [
        (STATUS_PENDING, True),
        (STATUS_RENDERING, True),
        (STATUS_FAILED, True),
        (STATUS_DONE, False),
    ])
    def test_is_restorable_by_status(self, status: str, expected: bool) -> None:
        assert BatchQueueItem(file_path="/x", status=status).is_restorable \
            is expected


class TestSaveLoad:
    def test_save_then_load_roundtrip(self, queue_env) -> None:
        items = _make_items()
        settings = {"format": "topos", "fps": 24}
        assert save_batch_queue(items, settings) is True
        assert queue_env.exists()

        loaded, loaded_settings = load_batch_queue()
        assert loaded == items
        assert loaded_settings == settings

    def test_written_payload_structure(self, queue_env) -> None:
        save_batch_queue([BatchQueueItem(file_path="/m.mov")], {"k": 1})
        payload = json.loads(queue_env.read_text(encoding="utf-8"))
        assert set(payload.keys()) == {"items", "settings"}
        assert payload["items"][0]["file_path"] == "/m.mov"

    def test_save_without_settings_preserves_stored_settings(
            self, queue_env) -> None:
        """AUD-33115：settings=None 不覆盖已存设置。"""
        save_batch_queue([BatchQueueItem(file_path="/a")], {"fmt": "png"})
        save_batch_queue([BatchQueueItem(file_path="/b")], None)

        _, settings = load_batch_queue()
        assert settings == {"fmt": "png"}

    def test_save_without_settings_and_no_prior_file(
            self, queue_env) -> None:
        save_batch_queue([BatchQueueItem(file_path="/a")], None)
        items, settings = load_batch_queue()
        assert len(items) == 1
        assert settings is None

    def test_save_explicit_none_settings_overrides_previous(
            self, queue_env) -> None:
        """显式传空 dict（非 None）允许清空设置。"""
        save_batch_queue([BatchQueueItem(file_path="/a")], {"old": True})
        save_batch_queue([BatchQueueItem(file_path="/a")], {})
        _, settings = load_batch_queue()
        assert settings == {}

    def test_save_with_corrupted_old_file_still_saves(self, queue_env) -> None:
        """旧文件损坏（settings 继承失败）不阻塞本次保存。"""
        queue_env.write_text("{ not json", encoding="utf-8")
        assert save_batch_queue([BatchQueueItem(file_path="/a")]) is True
        items, _ = load_batch_queue()
        assert [i.file_path for i in items] == ["/a"]

    def test_save_failure_returns_false(self, monkeypatch) -> None:
        """config 目录不可创建（/dev/null 是字符设备非目录）→ False 不抛。"""
        monkeypatch.setenv("TOPOS_CONFIG_PATH", "/dev/null/config.json")
        assert save_batch_queue([BatchQueueItem(file_path="/a")]) is False


class TestLoadEdgeCases:
    def test_load_missing_file_returns_empty(self, queue_env) -> None:
        assert load_batch_queue() == ([], None)

    def test_load_corrupted_file_returns_empty(self, queue_env) -> None:
        queue_env.write_text("]]] broken", encoding="utf-8")
        assert load_batch_queue() == ([], None)

    def test_load_file_without_items_key(self, queue_env) -> None:
        queue_env.write_text(json.dumps({"settings": {"f": 1}}),
                             encoding="utf-8")
        items, settings = load_batch_queue()
        assert items == []
        assert settings == {"f": 1}


class TestClearAndRestore:
    def test_clear_removes_file(self, queue_env) -> None:
        save_batch_queue(_make_items())
        assert queue_env.exists()
        clear_batch_queue()
        assert not queue_env.exists()
        assert load_batch_queue() == ([], None)

    def test_clear_without_file_is_noop(self, queue_env) -> None:
        clear_batch_queue()  # 不应抛异常
        assert not queue_env.exists()

    def test_has_restorable_items_true_when_unfinished(self, queue_env) -> None:
        save_batch_queue([BatchQueueItem(file_path="/a", status=STATUS_DONE),
                          BatchQueueItem(file_path="/b", status=STATUS_PENDING)])
        assert has_restorable_items() is True

    def test_has_restorable_items_false_when_all_done(
            self, queue_env) -> None:
        save_batch_queue([BatchQueueItem(file_path="/a", status=STATUS_DONE)])
        assert has_restorable_items() is False

    def test_has_restorable_items_false_without_file(
            self, queue_env) -> None:
        assert has_restorable_items() is False
