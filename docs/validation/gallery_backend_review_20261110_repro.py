"""复查证据：全部文件操作限于 TemporaryDirectory，不读取用户画廊。

仓库根目录执行：
  .venv/bin/python docs/validation/gallery_backend_review_20261110_repro.py

每项 finding=true 表示仍存在问题；修复后可加 --strict 作为验收门。
这是故障注入诊断脚本，不把已知未修复问题加入默认 pytest 套件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.color.core import gallery_bundle_service as bundles
from src.features.color.core import still_capture_transaction as captures
from src.features.color.core.gallery_capture_service import (
    GalleryCaptureFacts, GalleryCaptureSession,
)
from src.features.color.core.gallery_contracts import (
    GalleryCaptureRequest, GalleryOperationResult, GalleryQuery,
)
from src.features.color.core.gallery_query_service import GalleryQueryService
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import GalleryTransferService
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta


def setup(base: Path):
    root = base / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir()
    repository = GalleryFolderRepository(str(root))
    return root, bundles.GalleryBundleService(str(root)), GalleryTransferService(repository)


def seed(folder: Path, stem: str):
    with zipfile.ZipFile(folder / f"{stem}.topos", "w") as package:
        package.writestr("project.json", json.dumps({"layers": [{"name": "grade"}]}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"NEW THUMBNAIL")
    save_preset_meta(str(folder / f"{stem}.topos"), PresetMeta(
        still_id="S-review-fixture", label="before"))


findings = []


def report(name: str, finding: bool, **facts):
    findings.append(finding)
    print(json.dumps(dict(case=name, finding=finding, **facts), ensure_ascii=False))


with tempfile.TemporaryDirectory() as directory:
    root, bundle_service, transfer = setup(Path(directory))
    user, target = root / "User", root / "System"
    seed(user, "Look")
    archive = root / "bundle.zip"
    assert transfer.export_bundle(str(user), "Look", str(archive)).succeeded
    replace = os.replace
    installs = [0]

    def fail_second_install(src, dst):
        if Path(os.path.realpath(dst)).parent == Path(os.path.realpath(target)):
            installs[0] += 1
            if installs[0] == 2:
                raise OSError("injected second install failure")
        return replace(src, dst)

    with patch("src.features.color.core.gallery_transfer_service.os.replace",
               side_effect=fail_second_install):
        result = transfer.import_bundle(str(archive), str(target), new_name="Partial")
    leftovers = sorted(path.name for path in target.iterdir())
    report("import_partial_install", bool(leftovers), status=result.status, leftovers=leftovers)
    orphan = target / "Taken.thumb.jpg"
    original = b"OLD USER THUMBNAIL"
    orphan.write_bytes(original)
    result = transfer.import_bundle(str(archive), str(target), new_name="Taken")
    preserved = orphan.read_bytes() == original
    report("import_orphan_overwrite", not preserved, status=result.status,
           old_bytes_preserved=preserved)

with tempfile.TemporaryDirectory() as directory:
    root, bundle_service, _ = setup(Path(directory))
    user = root / "User"
    source = user / "Old.topos"
    source.write_bytes(b"GRADE")
    write = bundles._MoveTransaction._write

    def die_between_replace_and_journal(self):
        if any(entry.get("installed") for entry in self.entries):
            raise KeyboardInterrupt("simulated process death before journal update")
        return write(self)

    try:
        with patch.object(bundles._MoveTransaction, "_write", die_between_replace_and_journal):
            bundle_service.rename_bundle(str(user), "Old", "New")
    except KeyboardInterrupt:
        pass
    bundle_service.recover_gallery_transactions(str(user))
    report("move_crash_window", not source.exists(), old_exists=source.exists(),
           new_exists=(user / "New.topos").exists(),
           journals_left=len(list(user.glob(bundles.TXN_DIR_PREFIX + "*"))))

with tempfile.TemporaryDirectory() as directory:
    root, bundle_service, _ = setup(Path(directory))
    user = root / "User"
    txn = user / (bundles.TXN_DIR_PREFIX + "fault")
    txn.mkdir()
    staged = txn / "Old.topos"
    staged.write_bytes(b"ONLY COPY")
    journal = txn / bundles.MANIFEST_NAME
    journal.write_text(json.dumps({"state": bundles.STATE_INSTALLING, "entries": [{
        "src": str(user / "Old.topos"), "staged": str(staged),
        "dst": str(user / "New.topos"), "installed": False}]}))
    with patch("src.features.color.core.gallery_bundle_service.os.replace",
               side_effect=OSError("restore device unavailable")):
        bundle_service.recover_gallery_transactions(str(user))
    report("move_failed_recovery", not staged.exists(),
           only_copy_preserved=staged.exists(), journal_preserved=journal.exists())

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory) / "Old.topos"
    target.write_bytes(b"OLD GRADE")
    txn = captures.StillCaptureTransaction(directory)
    Path(txn.staged_path(str(target))).write_bytes(b"NEW GRADE")
    txn.add(str(target))
    write = captures.StillCaptureTransaction._write_manifest

    def die_after_backup(self):
        if any(entry.get("backed_up") for entry in self._entries):
            raise KeyboardInterrupt("simulated process death before backup journal update")
        return write(self)

    try:
        with patch.object(captures.StillCaptureTransaction, "_write_manifest", die_after_backup):
            txn.commit()
    except KeyboardInterrupt:
        pass
    captures.recover_still_transactions(directory)
    preserved = target.exists() and target.read_bytes() == b"OLD GRADE"
    report("capture_backup_crash_window", not preserved, old_bytes_preserved=preserved,
           transaction_preserved=Path(txn.txn_dir).exists())

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory) / "Old.topos"
    target.write_bytes(b"OLD GRADE")
    txn = captures.StillCaptureTransaction(directory)
    Path(txn.staged_path(str(target))).write_bytes(b"NEW GRADE")
    txn.add(str(target))
    txn._commit_backing()
    with patch("src.features.color.core.still_capture_transaction.os.replace",
               side_effect=OSError("restore device unavailable")):
        try:
            txn.rollback()
        except OSError:
            pass
    preserved = target.exists() or Path(txn.backup_dir, target.name).exists()
    report("capture_failed_rollback", not preserved, old_copy_preserved=preserved,
           transaction_preserved=Path(txn.txn_dir).exists())

with tempfile.TemporaryDirectory() as directory:
    from src.app.controllers.layer_controller import LayerOpsMixin

    root, _, _ = setup(Path(directory))
    current = [str(root / "User")]
    request = GalleryCaptureRequest(request_id="review-capture", album_id="legacy:User")
    facts = GalleryCaptureFacts(album_id="legacy:User", grade_revision=1)
    written = []
    controller = LayerOpsMixin.__new__(LayerOpsMixin)
    controller.presets_panel = SimpleNamespace(
        get_current_folder=lambda: current[0],
        _repository=GalleryFolderRepository(str(root)))
    controller._new_still_name = lambda: "Capture"
    controller._commit_still_capture = lambda path, *_: written.append(path) or True
    captured = {}

    def prepare():
        current[0] = str(root / "System")
        return object()

    result = GalleryCaptureSession(request, facts, lambda: facts).run(
        prepare, lambda image: controller._persist_still_capture(
            image, False, captured, request=request))
    wrong_target = bool(written) and Path(written[0]).parent == root / "System"
    report("capture_destination_not_locked", wrong_target, status=result.status,
           requested_album=request.album_id,
           actual_folder=Path(written[0]).parent.name if written else "none")
    result = GalleryCaptureSession(request, facts, lambda: GalleryCaptureFacts(
        album_id="legacy:User", grade_revision=2)).run(
        object, lambda _: GalleryOperationResult(request_id=request.request_id, status="success"))
    report("capture_grade_revision_not_checked", result.succeeded, status=result.status)

with tempfile.TemporaryDirectory() as directory:
    root, _, _ = setup(Path(directory))
    user = root / "User"
    seed(user, "Look")
    service = GalleryQueryService(GalleryFolderRepository(str(root)))
    query = GalleryQuery(album_id="legacy:User")
    service.query(query)
    meta_path = user / "Look.meta.json"
    meta = json.loads(meta_path.read_text())
    meta["label"] = "after"
    meta_path.write_text(json.dumps(meta))  # 原地写，不改变目录条目。
    returned = service.query(query).stills[0].meta.label
    report("in_place_meta_cache_stale", returned != "after", disk_label="after", returned_label=returned)

if "--strict" in sys.argv and any(findings):
    raise SystemExit(1)
