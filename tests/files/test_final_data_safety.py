"""Final native Files audit: actual writes, recovery and lifecycle boundaries."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.services.facade import FilesFacade
from src.features.files.transactions import FileOperationService, FileTransactionService, OperationJournal
from src.features.files.transactions.batch_plan import BatchRenamePlanner, PlanError
from src.features.files.transactions import ops


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / 'config'))
    media = tmp_path / 'media'
    media.mkdir()
    library = catalog.register_library(media)
    yield catalog, media, library.library_id
    catalog.close()


@pytest.mark.parametrize('policy', ['error', 'overwrite'])
def test_copy_produces_independent_inode_and_preserves_source(tmp_path, policy):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'source')
    destination = tmp_path / 'destination'
    destination.mkdir()
    if policy == 'overwrite':
        (destination / source.name).write_bytes(b'old')
    result = FileTransactionService().copy(source, destination, policy=policy)
    assert result.ok
    output = destination / source.name
    assert source.read_bytes() == b'source'
    assert source.stat().st_ino != output.stat().st_ino
    output.write_bytes(b'edited copy')
    assert source.read_bytes() == b'source'


def test_directory_no_replace_rejects_target_created_at_publication(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_bytes(b'payload')
    parent = tmp_path / 'destination'
    parent.mkdir()
    actual = ops._rename_noreplace

    def race(src, dst):
        dst.mkdir()
        return actual(src, dst)

    monkeypatch.setattr(ops, '_rename_noreplace', race)
    result = FileTransactionService().move(source, parent)
    assert not result.ok and result.code == 'DEST_EXISTS'
    assert (source / 'file').read_bytes() == b'payload'
    assert (parent / 'source').is_dir() and not list((parent / 'source').iterdir())


def test_same_file_overwrite_is_refused(tmp_path):
    source = tmp_path / 'file'
    source.write_bytes(b'original')
    result = FileTransactionService().copy(source, tmp_path, policy='overwrite')
    assert not result.ok and result.code == 'SAME_FILE'
    assert source.read_bytes() == b'original'


def test_no_operation_follows_a_linked_parent(tmp_path):
    source = tmp_path / 'file'
    source.write_bytes(b'original')
    outside = tmp_path / 'outside'
    outside.mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(outside, target_is_directory=True)
    result = FileTransactionService().copy(source, linked)
    assert not result.ok and result.code == 'PARENT_SYMLINK'
    assert not list(outside.iterdir())


def test_directory_cross_volume_move_uses_complete_staging(tmp_path, monkeypatch):
    source = tmp_path / 'bundle'
    source.mkdir()
    (source / 'child').write_bytes(b'payload')
    target = tmp_path / 'out'
    target.mkdir()
    monkeypatch.setattr(ops, 'same_volume', lambda *_: False)
    result = FileTransactionService().move(source, target)
    assert result.ok and not source.exists()
    assert (target / 'bundle' / 'child').read_bytes() == b'payload'
    assert sorted(p.name for p in target.iterdir()) == ['bundle']


def test_trash_snapshot_insert_failure_cannot_move_source(env):
    catalog, media, library = env
    source = media / 'keep'
    source.write_bytes(b'keep')
    with catalog._write() as cursor:
        cursor.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON trash_items BEGIN SELECT RAISE(ABORT,'snapshot unavailable'); END")
    with pytest.raises(sqlite3.Error):
        FileOperationService(catalog).trash(library, [source])
    assert source.read_bytes() == b'keep'
    assert not list(catalog.locator.trash_dir(library).iterdir())


def test_directory_trash_restores_every_row_and_exact_relationships(env):
    catalog, media, library = env
    folder = media / 'bundle'
    folder.mkdir()
    source = folder / 'child'
    source.write_bytes(b'child')
    catalog.upsert_asset(library, folder, is_dir=1)
    catalog.upsert_asset(library, source, size=5)
    catalog.patch_user_fields(library, source, rating=5, comment='exact')
    tag = catalog.create_tag(library, 'parent')
    catalog.tag_asset(library, source, tag)
    collection = catalog.create_collection(library, 'sequence')
    catalog.add_to_collection(collection, source)
    with catalog._write() as cursor:
        cursor.execute('UPDATE collection_items SET sort_order=17,added_at=123 WHERE collection_id=?', (collection,))
        cursor.execute('INSERT INTO stacks(library_id,cover_path,created_at) VALUES(?,?,456)', (library,str(source)))
        stack = cursor.lastrowid
        cursor.execute('INSERT INTO stack_members(stack_id,path) VALUES(?,?)', (stack,str(source)))
        cursor.execute("UPDATE assets_fts SET ocr='searchable words' WHERE library_id=? AND path=?", (library,str(source)))
    before = catalog.snapshot_relations(library,source)
    service = FileOperationService(catalog)
    deleted = service.trash(library,[folder])[0]
    assert deleted.ok and catalog.asset(library,source) is None
    restored = service.restore(library,deleted.record_id)
    assert restored.ok and catalog.asset(library,source)['rating'] == 5
    assert catalog.snapshot_relations(library,source) == before
    assert catalog.asset(library,folder)['is_dir'] == 1


def test_same_named_trash_entries_restore_original_name_without_serial_suffix(env):
    catalog, media, library = env
    first = media / 'one'
    second = media / 'two'
    first.mkdir(); second.mkdir()
    a,b = first/'item',second/'item'
    a.write_bytes(b'a');b.write_bytes(b'b')
    service = FileOperationService(catalog)
    records = service.trash(library,[a,b])
    assert all(r.ok for r in records)
    assert all(service.restore(library,r.record_id).ok for r in records)
    assert a.read_bytes() == b'a' and b.read_bytes() == b'b'


def test_restore_database_failure_rolls_file_back_for_retry(env,monkeypatch):
    catalog, media, library = env
    source = media/'item';source.write_bytes(b'item')
    catalog.upsert_asset(library,source)
    service=FileOperationService(catalog)
    record=service.trash(library,[source])[0]
    actual=catalog.insert_asset_full
    monkeypatch.setattr(catalog,'insert_asset_full',lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError('db failed')))
    result=service.restore(library,record.record_id)
    assert not result.ok and not source.exists()
    assert len(service.trash_service.list_items(library))==1
    monkeypatch.setattr(catalog,'insert_asset_full',actual)
    assert service.restore(library,record.record_id).ok
    assert source.read_bytes()==b'item'


def test_batch_plan_rejects_same_size_mtime_replacement(env):
    catalog,media,library=env
    source=media/'a';source.write_bytes(b'old')
    planner=BatchRenamePlanner(catalog)
    plan=planner.preview(library,[source],'replace',{'find':'a','replace':'b'})
    original=source.stat()
    replacement=media/'new';replacement.write_bytes(b'new')
    os.utime(replacement,ns=(original.st_atime_ns,original.st_mtime_ns))
    os.replace(replacement,source)
    with pytest.raises(PlanError,match='变化'):
        planner.execute(plan.plan_id,library)
    assert source.read_bytes()==b'new' and not (media/'b').exists()


def test_batch_dto_mutation_and_cross_library_cannot_change_confirmed_plan(env):
    catalog,media,library=env
    source=media/'a';source.write_bytes(b'a')
    planner=BatchRenamePlanner(catalog)
    plan=planner.preview(library,[source],'replace',{'find':'a','replace':'b'})
    plan.items[0].proposed_name='evil'
    with pytest.raises(PlanError,match='素材库'):
        planner.execute(plan.plan_id,'foreign')
    assert planner.execute(plan.plan_id,library)[0].ok
    assert (media/'b').exists() and not (media/'evil').exists()


def test_batch_destination_change_is_rejected_before_any_item_moves(env):
    catalog,media,library=env
    paths=[media/'a',media/'c']
    for path in paths:path.write_bytes(b'content')
    planner=BatchRenamePlanner(catalog)
    plan=planner.preview(library,paths,'sequence',{'prefix':'final-'})
    (media/'final-002').write_bytes(b'new occupant')
    with pytest.raises(PlanError,match='占用'):
        planner.execute(plan.plan_id,library)
    assert all(path.exists() for path in paths)
    assert not (media/'final-001').exists()


def test_metadata_failure_remains_recoverable_not_completed(env,monkeypatch):
    catalog,media,library=env
    source=media/'a';source.write_bytes(b'a')
    catalog.upsert_asset(library,source)
    journal=OperationJournal(catalog)
    service=FileOperationService(catalog,journal=journal)
    original=service._propagation.on_moved
    monkeypatch.setattr(service._propagation,'on_moved',lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    result=service.rename(library,source,'b')
    assert not result.ok and result.code=='METADATA_PENDING_RECOVERY'
    job=journal.pending()[0]
    assert job['state']=='pending_recovery'
    monkeypatch.setattr(service._propagation,'on_moved',original)
    assert journal.recover_one(job['id'])['state']=='completed'
    assert catalog.asset(library,media/'b') is not None
    assert catalog.asset(library,source) is None


def test_facade_has_bounded_admission_and_nonblocking_drained_close(env):
    catalog,media,library=env
    facade=FilesFacade(catalog,max_workers=1,max_pending=2)
    gate=threading.Event();entered=threading.Event();drained=threading.Event()
    def work():entered.set();gate.wait(3);return 1
    outcomes=[]
    facade.run('one','one',work,outcomes.append)
    assert entered.wait(1)
    facade.run('two','two',lambda:2,outcomes.append)
    facade.run('three','three',lambda:3,outcomes.append)
    assert len(outcomes)==1 and outcomes[0].error.code=='busy'
    started=time.monotonic();facade.shutdown()
    assert time.monotonic()-started<0.2
    facade.when_idle(drained.set)
    assert not drained.is_set()
    gate.set()
    assert drained.wait(2)
    assert len(outcomes)==1


def test_catalog_instances_share_revision_and_maintenance_lock(env):
    catalog,media,library=env
    other=FilesCatalog(catalog.locator)
    try:
        assert other._lock is catalog._lock
        before=other.revision
        catalog.upsert_asset(library,media/'a')
        assert other.revision==catalog.revision>before
    finally:other.close()


@pytest.mark.parametrize('library',['../escape','/tmp/x','','a/b','a\\b'])
def test_trash_library_identifier_cannot_escape_root(tmp_path,library):
    with pytest.raises(ValueError):FilesDataLocator.for_config_root(tmp_path).trash_dir(library)


def test_catalog_root_link_is_rejected_before_database_open(tmp_path):
    outside=tmp_path/'outside';outside.mkdir()
    config=tmp_path/'config';config.mkdir()
    (config/'files-data').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):FilesCatalog(FilesDataLocator.for_config_root(config))
    assert not list(outside.iterdir())
