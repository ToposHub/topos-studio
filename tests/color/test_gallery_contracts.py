"""G01 Gallery 领域合同测试：DTO 校验／身份口径／能力注册表单一事实源。

对应计划 docs/plans/2026-11-09-color-gallery-backend-plan.md §4/§5 G01：
- 门槛"无素材 Capture 禁用"；
- "仅 JPEG 且无有效 grade 时可 reference 但不可 Apply"；
- 未实现服务（import／label／split／集合／Memories）如实 False＋稳定
  reason，不冒充已接线。
"""
import pytest

from src.features.color.core.gallery_contracts import (
    ALBUM_KIND_STILLS,
    BUNDLE_ROLE_EXR_DECLARATION,
    BUNDLE_ROLE_GRADE,
    BUNDLE_SUFFIX_BY_ROLE,
    GalleryAlbum,
    GalleryBundleRef,
    GalleryCapabilityContext,
    GalleryCaptureRequest,
    GalleryOperationResult,
    GalleryQuery,
    GalleryStill,
    GalleryStillProvenance,
    OP_CANCELLED,
    OP_REJECTED,
    OP_SUCCESS,
    SCOPE_LEGACY_FOLDER,
    SORT_KEY_CREATED,
    make_gallery_request_id,
    evaluate_gallery_capabilities,
    gallery_capability_by_action,
)


def _album(read_only: bool = False) -> GalleryAlbum:
    return GalleryAlbum(
        album_id="legacy:User", scope=SCOPE_LEGACY_FOLDER,
        kind=ALBUM_KIND_STILLS, label="User", read_only=read_only)


def _still(*, has_exr=False, has_still_jpeg=True, has_thumbnail=True,
           grade_loadable=None) -> GalleryStill:
    return GalleryStill(
        still_id="abc123", album_id="legacy:User",
        bundle=GalleryBundleRef(topos_name="Still_1.topos"),
        display_name="Still_1",
        has_exr=has_exr, has_still_jpeg=has_still_jpeg,
        has_thumbnail=has_thumbnail, grade_loadable=grade_loadable)


def _caps(context: GalleryCapabilityContext):
    return gallery_capability_by_action(
        evaluate_gallery_capabilities(context))


# ── DTO 校验 ───────────────────────────────────────────────────────────

class TestDtoValidation:
    def test_album_rejects_unknown_scope_and_kind(self):
        with pytest.raises(ValueError):
            GalleryAlbum(album_id="a", scope="bogus", kind="stills", label="A")
        with pytest.raises(ValueError):
            GalleryAlbum(album_id="a", scope="global", kind="bogus", label="A")
        with pytest.raises(ValueError):
            GalleryAlbum(album_id="", scope="global", kind="stills", label="A")

    def test_bundle_ref_rejects_path_separators(self):
        for bad in ("../x.topos", "a/b.topos", "a\\b.topos"):
            with pytest.raises(ValueError):
                GalleryBundleRef(topos_name=bad)
        ref = GalleryBundleRef(topos_name="Still_1.topos")
        assert ref.stem() == "Still_1"
        assert ref.sidecar_name(BUNDLE_ROLE_EXR_DECLARATION) == \
            "Still_1.still.exr.json"

    def test_bundle_suffixes_cover_six_roles_including_exr_declaration(self):
        # 旧 _SIDECAR_SUFFIXES 遗漏 .still.exr.json；合同层必须完整。
        assert set(BUNDLE_SUFFIX_BY_ROLE.values()) == {
            ".topos", ".thumb.jpg", ".still.jpg", ".meta.json",
            ".still.exr", ".still.exr.json"}

    def test_query_rejects_unknown_sort_key(self):
        with pytest.raises(ValueError):
            GalleryQuery(album_id="legacy:User", sort_key="bogus")
        q = GalleryQuery(album_id="legacy:User", sort_key=SORT_KEY_CREATED,
                         sort_desc=True, request_revision=7)
        assert q.to_dict()["request_revision"] == 7

    def test_operation_result_rejects_unknown_status(self):
        with pytest.raises(ValueError):
            GalleryOperationResult(request_id="r1", status="bogus")
        ok = GalleryOperationResult(request_id="r1", status=OP_SUCCESS,
                                    affected_still_ids=("s1",))
        assert ok.succeeded and ok.affected_still_ids == ("s1",)
        assert not GalleryOperationResult(
            request_id="r2", status=OP_REJECTED).succeeded
        assert not GalleryOperationResult(
            request_id="r3", status=OP_CANCELLED).succeeded

    def test_capture_request_cancel_token_and_request_id(self):
        req = GalleryCaptureRequest(
            request_id=make_gallery_request_id(), album_id="legacy:User",
            source_frame=42, expected_target_revision=3,
            cancel_token="tok-1")
        assert req.cancelled("tok-1") is True
        assert req.cancelled("tok-2") is False
        assert req.cancelled(None) is False
        assert req.source_frame == 42
        another = make_gallery_request_id()
        assert another != req.request_id and len(another) == 32

    def test_provenance_from_meta_carries_source_fields(self):
        class _Meta:
            clip_id = "c1"
            asset_id = "a1"
            display_name = "A001.mov"
            source_frame = 88
            source_transfer = "pq"
            timeline_frame = None
        prov = GalleryStillProvenance.from_preset_meta(_Meta())
        assert prov.clip_id == "c1" and prov.source_frame == 88
        assert prov.source_display_name == "A001.mov"
        empty = GalleryStillProvenance.from_preset_meta(None)
        assert empty.clip_id is None and empty.source_frame is None


# ── 能力注册表 ──────────────────────────────────────────────────────────

class TestCapabilityMatrix:
    def test_empty_context_disables_everything_with_reasons(self):
        caps = _caps(GalleryCapabilityContext())
        assert caps["capture.still"].supported is False
        assert caps["capture.still"].reason  # 稳定非空 reason
        assert caps["apply.grade"].supported is False
        assert caps["reference.load"].supported is False
        for key in ("import.bundle", "label.rename", "compare.split",
                    "album.powergrade", "album.timelines", "album.memories"):
            assert caps[key].supported is False
            assert caps[key].reason

    def test_plan_threshold_no_media_capture_disabled(self):
        """G01 门槛：无素材 Capture 禁用（reason=no_media）。"""
        caps = _caps(GalleryCapabilityContext(
            has_media=False, engine_available=True, album=_album()))
        assert caps["capture.still"].supported is False
        assert caps["capture.still"].reason

    def test_media_and_engine_and_writable_album_enable_capture(self):
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album()))
        assert caps["capture.still"].supported is True
        assert caps["capture.still"].reason is None

    def test_missing_engine_disables_capture_with_engine_reason(self):
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=False, album=_album()))
        assert caps["capture.still"].supported is False
        assert "engine" in caps["capture.still"].reason.lower()

    def test_readonly_album_disables_write_actions(self):
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True,
            album=_album(read_only=True), selection=_still(),
            selection_grade_loadable=True))
        for key in ("capture.still", "album.create", "rename.bundle",
                    "delete.bundle"):
            assert caps[key].supported is False
        # 读操作不受 album 只读影响
        assert caps["reference.load"].supported is True
        assert caps["apply.grade"].supported is True
        assert caps["export.thumbnail"].supported is True

    def test_plan_threshold_jpeg_only_unloadable_grade_reference_yes_apply_no(self):
        """G01 门槛：仅 JPEG 且 grade 不可加载 → 可 reference，不可 Apply。"""
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album(),
            selection=_still(grade_loadable=False),
            selection_grade_loadable=False))
        assert caps["reference.load"].supported is True
        assert caps["compare.wipe"].supported is True
        assert caps["match.color"].supported is True
        assert caps["apply.grade"].supported is False
        assert caps["apply.categories"].supported is False
        assert "grade" in caps["apply.grade"].reason.lower()

    def test_unprobed_grade_treated_as_not_verified(self):
        """查询路径不默认探测 grade：None ≠ 可加载，Apply 按未证实禁用。"""
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album(),
            selection=_still(grade_loadable=None),
            selection_grade_loadable=None))
        assert caps["apply.grade"].supported is False

    def test_no_selection_disables_item_actions(self):
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album(),
            selection=None, selection_count=0))
        for key in ("apply.grade", "reference.load", "rename.bundle",
                    "delete.bundle", "export.thumbnail", "match.color"):
            assert caps[key].supported is False

    def test_selection_without_any_image_disables_reference(self):
        still = GalleryStill(
            still_id="x", album_id="legacy:User",
            bundle=GalleryBundleRef(topos_name="bare.topos"),
            display_name="bare", has_exr=False, has_still_jpeg=False,
            has_thumbnail=False, grade_loadable=True)
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album(),
            selection=still, selection_grade_loadable=True))
        assert caps["apply.grade"].supported is True
        assert caps["reference.load"].supported is False
        assert caps["match.color"].supported is False

    def test_no_media_target_disables_apply_and_reference(self):
        caps = _caps(GalleryCapabilityContext(
            has_media=False, engine_available=True, album=_album(),
            selection=_still(grade_loadable=True),
            selection_grade_loadable=True))
        assert caps["apply.grade"].supported is False
        assert caps["reference.load"].supported is False

    def test_export_thumbnail_requires_thumb(self):
        no_thumb = _still(has_thumbnail=False, has_still_jpeg=True)
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album(),
            selection=no_thumb, selection_grade_loadable=True))
        assert caps["export.thumbnail"].supported is False

    def test_unimplemented_services_stay_disabled_even_with_everything(self):
        """服务位未落地（默认 False）时即使有媒体/选中也不冒充可用。"""
        caps = _caps(GalleryCapabilityContext(
            has_media=True, engine_available=True, album=_album(),
            selection=_still(grade_loadable=True),
            selection_grade_loadable=True))
        for key, token in (("import.bundle", "import"),
                           ("label.rename", "label"),
                           ("compare.split", "split"),
                           ("album.powergrade", "collection"),
                           ("album.memories", "memories")):
            assert caps[key].supported is False
            assert token in caps[key].reason.lower()
