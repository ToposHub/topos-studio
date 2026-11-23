"""GPU ownership ordering, retirement and sync-object fallback checks."""
from unittest.mock import Mock

import pytest

from src.shared.gpu.scope_publication import ScopePublication
from src.shared.gpu.gpu_scope_engine import GpuScopeEngine


@pytest.fixture
def sync(monkeypatch):
    import OpenGL.GL as gl
    calls = []
    fences = iter((11, 12, 13, 14, 15))
    monkeypatch.setattr(gl, 'glFenceSync', lambda *_: next(fences))
    monkeypatch.setattr(gl, 'glFlush', lambda: calls.append(('flush',)))
    monkeypatch.setattr(gl, 'glWaitSync', lambda fence, flags, timeout: calls.append(('wait', fence, flags, timeout)))
    monkeypatch.setattr(gl, 'glDeleteSync', lambda fence: calls.append(('delete', fence)))
    monkeypatch.setattr(gl, 'glFinish', lambda: calls.append(('finish',)))
    return calls


def test_publication_orders_producer_and_multiple_consumer_contexts(sync):
    finish = Mock()
    publication = ScopePublication.publish(finish)
    assert publication.wait_ready()
    publication.copied()
    assert publication.wait_ready()
    publication.copied()
    publication.prepare_write()
    assert publication.valid
    assert not publication.wait_ready(), 'Unpublished/failed writes must not be copied'
    # Each reader is flushed; producer waits on both contexts before reusing.
    assert sync == [
        ('flush',), ('wait', 11, 0, 0xFFFFFFFFFFFFFFFF), ('flush',),
        ('wait', 11, 0, 0xFFFFFFFFFFFFFFFF), ('flush',),
        ('wait', 12, 0, 0xFFFFFFFFFFFFFFFF), ('delete', 12),
        ('wait', 13, 0, 0xFFFFFFFFFFFFFFFF), ('delete', 13), ('delete', 11),
    ]
    publication.refresh(finish)
    assert publication.wait_ready()
    assert sync[-1] == ('wait', 14, 0, 0xFFFFFFFFFFFFFFFF)
    publication.retire()
    assert not publication.wait_ready()
    before = list(sync)
    publication.retire()
    assert sync == before
    finish.assert_not_called()


@pytest.mark.parametrize('raises', [False, True])
def test_sync_unavailable_uses_correctness_fallback(sync, monkeypatch, raises):
    import OpenGL.GL as gl
    fence = Mock(side_effect=RuntimeError('unavailable')) if raises else Mock(return_value=None)
    monkeypatch.setattr(gl, 'glFenceSync', fence)
    finish = Mock()
    publication = ScopePublication.publish(finish)
    finish.assert_called_once()
    assert publication.wait_ready()
    publication.copied()
    assert ('finish',) in sync
    publication.retire()
    assert not publication.wait_ready()


def test_pool_reallocation_revokes_old_publication_before_id_reuse(sync):
    ctx = Mock()
    engine = GpuScopeEngine(ctx)
    engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    publication = engine.publish()
    engine.begin_frame()
    assert publication.valid
    assert engine.ensure_pool((320, 180), (1024, 300), (800, 800)) is engine._pool
    assert engine.publish() is publication
    engine.ensure_pool((512, 288), (1024, 300), (800, 800))
    assert not publication.valid
    replacement = engine.publish()
    assert replacement is not publication and replacement.valid
    engine.release()
    assert not replacement.valid


def test_flush_failure_releases_created_sync_before_fallback(sync, monkeypatch):
    import OpenGL.GL as gl
    monkeypatch.setattr(gl, 'glFlush', Mock(side_effect=RuntimeError('flush failed')))
    finish = Mock()
    publication = ScopePublication.publish(finish)
    assert ('delete', 11) in sync
    finish.assert_called_once()
    assert publication.wait_ready()
    publication.copied()
    assert ('delete', 12) in sync and ('finish',) in sync
    publication.retire()
