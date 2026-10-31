"""O5（复验 2026-08-31）：capability manifest 单一真相源一致性门。

R-28 根因 = 能力声明在 spec/native query/Python summary/UI/SDK/计划表
多处手抄后漂移。本测试把 ``native/topos_codec/docs/capability_manifest.json`` 钉为
唯一真相源，逐项校验各消费面与之一致：

- topos_profiles（tiers/is_preset/native_profile/label/available）
- topos_binding.TOPOS_MOVIE_CAPS（audio/profile/pixel formats/alpha）
- UI 登记（Quick Export 容器表 / Deliver 面板目录 / 容器矩阵 / i18n key）
- native query（需 lib：profile×pf×bd 支持矩阵 ↔ manifest）
- ABI 版本（binding 常量 ↔ manifest）
- perf 门禁脚本阈值 ↔ manifest

任何能力变更必须先改 manifest（+对应面），本测试即红——先红后绿的
漂移防线（复验 §5 O5 任务 2）。
"""
from __future__ import annotations

import json
import os
import re

import pytest

pytestmark = pytest.mark.media_core

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MANIFEST_PATH = os.path.join(_ROOT, "native", "topos_codec", "docs", "capability_manifest.json")


def _manifest() -> dict:
    with open(_MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    from src.shared.codec import topos_binding

    return any(p.is_file() for p in topos_binding.candidate_library_paths())


@pytest.fixture(scope="module")
def require_lib():
    if not _lib_available():
        pytest.skip("libtopos_codec 未构建")


@pytest.fixture(scope="module")
def manifest():
    return _manifest()


class TestCapabilityManifestConsistency:
    def test_manifest_exists_and_versioned(self, manifest) -> None:
        assert manifest["schema_version"] == 1
        assert manifest["codec"]["abi_version"] == 2

    def test_tiers_match_profiles_module(self, manifest) -> None:
        from src.shared.codec.topos_profiles import (
            AVAILABLE_TOPOS_TIERS, TOPOS_CODEC_LABEL, TOPOS_PROFILE_TIERS,
        )

        assert manifest["codec"]["label"] == TOPOS_CODEC_LABEL
        base_to_manifest = {
            "yuv422p": "yuv422", "yuv444p": "yuv444",
            "gbrp": "gbr", "topos_cfa": "cfa",
        }
        mt = {t["id"]: t for t in manifest["tiers"]}
        assert set(mt) == set(TOPOS_PROFILE_TIERS) == set(AVAILABLE_TOPOS_TIERS)
        for tid, t in mt.items():
            pt = TOPOS_PROFILE_TIERS[tid]
            assert t["native_profile"] == pt.native_profile, tid
            assert t["label"] == pt.label, tid
            assert t["is_preset"] == (pt.native_profile == 3), tid
            assert t["available"] == pt.available, tid
            # R7：档位格式域机器可读（UI pix_fmt 选择器单一来源交叉钉死）
            assert t["format_bases"] == list(pt.format_bases), tid
            native = manifest["bitstream_profiles"][str(pt.native_profile)]
            assert {base_to_manifest[base] for base in pt.format_bases}.issubset(
                native["pixel_formats"]), tid
            assert set(pt.bit_depths).issubset(native["bit_depths"]), tid

    def test_ui_codec_ids_drive_all_ui_registries(self, manifest) -> None:
        ids = manifest["ui_codec_ids"]

        # AUD-33301：Quick Export 不再持手抄 _CONTAINER_CODECS 副本，
        # 与 deliver 共用 allowed_codecs_for_container 单一真相源
        from src.features.deliver.core.video_export_formats import (
            allowed_codecs_for_container,
        )

        quick_mov = allowed_codecs_for_container("mov")
        for cid in ids:
            assert cid in quick_mov, f"Quick Export 缺 {cid}"

        from src.features.deliver.core.video_export_formats import (
            CODEC_CONTAINER_TABLE,
        )

        for cid in ids:
            assert cid in CODEC_CONTAINER_TABLE, cid
            assert CODEC_CONTAINER_TABLE[cid] == frozenset({"mov"}), cid

        from src.features.deliver.ui.video_export_settings_panel import (
            _CODEC_CATALOGUE,
        )

        cat = {e["codec"]: e for e in _CODEC_CATALOGUE}
        for cid in ids:
            assert cid in cat, cid
            assert cat[cid]["container"] == "mov"
            # v1.8（ADR-C056）：alpha 标志逐档对齐档位声明——lp 帧间档
            # 显式 no-alpha（§3.7），其余档全 True
            from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS
            tier_of = {"topos_proxy": "proxy", "topos_lt": "lt",
                       "topos": "standard", "topos_hq": "hq",
                       "topos_4444": "4444", "topos_4444xq": "4444xq",
                       "topos_lp": "lp", "topos_raw": "raw"}
            assert cat[cid]["alpha"] is TOPOS_PROFILE_TIERS[tier_of[cid]].alpha_supported, cid

    def test_i18n_keys_for_all_ui_ids(self, manifest) -> None:
        for loc in ("zh_CN", "en_US"):
            path = os.path.join(_ROOT, "src", "shared", "i18n", "locales",
                                f"{loc}.json")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            exp = data["export"]
            for cid in manifest["ui_codec_ids"]:
                key = f"codec_{cid}"
                assert key in exp and exp[key], f"{loc} 缺 {key}"

    def test_binding_caps_match_manifest(self, manifest) -> None:
        from src.shared.codec import topos_binding as tb

        caps = tb.TOPOS_MOVIE_CAPS
        # v1.1：audio_tracks 解冻（ADR-C051）；manifest audio 描述能力档
        assert caps["video"] is True
        assert int(tb.TOPOS_CODEC_ABI_VERSION) == manifest["codec"]["abi_version"]
        # alpha 位深域
        assert set(manifest["alpha"]["bit_depths"]) == {8, 10, 12, 16}

    def test_native_query_matches_manifest(self, manifest, require_lib) -> None:
        """profile × pixel_format × bit_depth 支持矩阵 ↔ manifest（native 权威）。"""
        from src.shared.codec.topos_binding import ToposCodec, ToposCodecError

        codec = ToposCodec()
        combos = set()
        for pid, p in manifest["bitstream_profiles"].items():
            for pf_name in p["pixel_formats"]:
                pfe = manifest["pixel_formats"][pf_name]["pf_enum"]
                for bd in p["bit_depths"]:
                    combos.add((int(pid), pfe, bd))
        ok = 0
        for profile, pf, bd in combos:
            try:
                cfg = codec.movie_config(64, 48, profile=profile,
                                         pixel_format=pf, bit_depth=bd)
            except ToposCodecError:
                continue
            st = codec.config_validate(
                _frame_cfg(codec, profile, pf, bd))
            assert st == 0, (profile, pf, bd)
            ok += 1
        assert ok == len(combos), "manifest 声明的全部组合必须被 native 接受"

    def test_reduced_decode_capabilities_match_manifest(self, manifest, require_lib) -> None:
        from src.shared.codec.topos_binding import ToposCodec

        declared = manifest["reduced_decode"]
        codec = ToposCodec()
        caps = codec.capabilities()
        assert caps.max_width == declared["max_source_width"]
        assert caps.max_height == declared["max_source_height"]
        assert caps.auto2k_max_dim == declared["auto2k_max_dim"]
        assert caps.fixed_reduced_decode
        assert caps.auto2k_decode
        assert caps.cpu_surface_output == declared["cpu_surface_output"]
        # P6 v2.1 后续（2026-09-22）：预览关滤波请求位的能力位同步
        assert caps.decode_no_deblock == declared["decode_no_deblock"]
        assert caps.host_visible_gpu_surface == declared["host_visible_gpu_surface"]
        assert caps.v7a_band_decode == declared["v7a_band_decode"]
        assert caps.v7b_scalable_base == declared["v7b_scalable_base"]
        # P0-04：四态发布状态必须与 manifest 同步——reference/experimental
        # 状态下 UI/CLI 不得把 opt-in 展示为 native scalable release。
        status = codec.scalable_status()
        assert status in declared["scalable_status_states"]["vocabulary"]
        assert status == declared["v7b_scalable_status"], (
            "native scalable status 与 manifest 漂移："
            f"native={status}, manifest={declared['v7b_scalable_status']}"
        )

    def test_perf_gate_thresholds_match_manifest(self, manifest) -> None:
        path = os.path.join(_ROOT, "native", "topos_codec", "scripts",
                            "perf_gate.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        thr = manifest["performance_gate"]["thresholds_v2"]
        mapping = [
            ("1080p_encode_p50_ms", ("1920x1080", "编码", "p50")),
            ("1080p_decode_p95_ms", ("1920x1080", "解码", "p95")),
            ("1080p_sized_p50_ms", ("1920x1080", "sized", "p50")),
            ("4k_encode_p50_ms", ("3840x2160", "编码", "p50")),
            ("4k_decode_p95_ms", ("3840x2160", "解码", "p95")),
            ("4k_sized_p50_ms", ("3840x2160", "sized", "p50")),
        ]
        import ast

        tree = ast.parse(src)
        literals = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", "") == "_THRESHOLDS":
                        for el in node.value.elts:
                            literals.append(ast.literal_eval(el))
        lit = {(g, m, s): v for g, m, s, v in literals}
        for key, combo in mapping:
            assert lit[combo] == thr[key], (key, lit.get(combo), thr[key])

    def test_container_boundary_flags_in_spec(self, manifest) -> None:
        spec = os.path.join(_ROOT, "native", "topos_codec", "docs", "container_spec_v1.md")
        with open(spec, encoding="utf-8") as fh:
            text = fh.read()
        assert manifest["container"]["mdat_required"]
        assert "bit3=ratio_saturated" in text
        assert "缺 mdat" in text or "mdat" in text

    def test_sdk_geometry_statement_covers_444_gbr(self, manifest) -> None:
        sdk = os.path.join(_ROOT, "native", "topos_codec", "docs", "SDK.md")
        with open(sdk, encoding="utf-8") as fh:
            text = fh.read()
        for pf in ("4:4:4", "GBR"):
            assert pf in text, f"SDK 未覆盖 {pf} 几何（P1-19）"


def _frame_cfg(codec, profile, pf, bd):
    import ctypes

    from src.shared.codec import topos_binding as tb

    fc = tb._CFrameConfig()
    fc.struct_size = ctypes.sizeof(tb._CFrameConfig)
    fc.abi_version = tb.TOPOS_CODEC_ABI_VERSION
    fc.visible_width, fc.visible_height = 64, 48
    fc.qp_base = 24
    fc.qmatrix_id = 1
    fc.profile = profile
    fc.pixel_format = pf
    fc.bit_depth = bd
    if bd == 16:
        # v1.7 宽域整数帧只能用 rans2；RAW 和 GBR/YUV 均同一约束。
        fc.reserved[0] = 8
    if pf == 3:
        # v1.6 TRAW 契约：qm0 冻结 + transfer 冻结对（12→LOG0/16→linear）
        # + matrix=0；bd16 须宽域熵（rans2/v8）
        fc.qmatrix_id = 0
        fc.color_transfer = 8 if bd == 16 else 20
    else:
        fc.qmatrix_id = 1
        fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 0 if pf in (2, 3) else 1
    fc.sar_num = fc.sar_den = 1
    return fc


class TestStageStatusManifest:
    """O5 任务 3：机器可读阶段状态 + prerequisite gate。"""

    def test_stage_status_dag_and_prerequisites(self) -> None:
        path = os.path.join(_ROOT, "native", "topos_codec", "docs", "stage_status.json")
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        rounds = st["rounds"]
        for rid, info in rounds.items():
            for pre in info.get("requires", []):
                assert pre in rounds, f"{rid} 依赖未登记的 {pre}"
                # prerequisite gate：前置未完成时本轮不得标记 complete
                if info.get("status") == "complete":
                    assert rounds[pre].get("status") == "complete", \
                        f"{rid} 标记 complete 但前置 {pre} 未完成（R-28 防线）"

    def test_no_v20_completion_claim_before_r8(self) -> None:
        path = os.path.join(_ROOT, "native", "topos_codec", "docs", "stage_status.json")
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        assert st.get("v2_0_complete") is False, \
            "R8 独立复审通过前不得恢复 V2.0 完成标记"

    def test_r0_r6_verdicts_match_re_review(self) -> None:
        """复验 §2 结论 ↔ stage_status（R3=conditional，其余 partial/未完成）。"""
        path = os.path.join(_ROOT, "native", "topos_codec", "docs", "stage_status.json")
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        r = st["rounds"]
        expect = {"R0": "partial", "R1": "partial", "R2": "incomplete",
                  "R3": "conditional", "R4": "partial", "R5": "partial",
                  "R6": "partial"}
        for rid, status in expect.items():
            assert r[rid]["review_verdict"] == status, \
                f"{rid} 复验结论应为 {status}，实际 {r[rid]['review_verdict']}"


class TestAdrRollup:
    """O5 任务 4：ADR supersession 元数据 + 每主题唯一当前决策。"""

    def test_superseded_adrs_carry_metadata(self) -> None:
        adr_dir = os.path.join(_ROOT, "native", "topos_codec", "docs")
        superseding = {}   # 被取代 ADR 文件 → 取代它的文件（来自 superseded-by）
        for name in sorted(os.listdir(adr_dir)):
            if not (name.startswith("ADR-C") and name.endswith(".md")):
                continue  # ADR-INDEX.md 是索引本身，不参与 supersede 检查
            with open(os.path.join(adr_dir, name), encoding="utf-8") as fh:
                head = fh.read(2000)
            if "superseded-by" in head or "部分被取代" in head:
                m = re.search(r"superseded-by.*?([\w\-]+\.md)", head)
                assert m, f"{name} 声明被取代但缺 superseded-by 目标文件"
                target = m.group(1)
                assert os.path.exists(os.path.join(adr_dir, target)), \
                    f"{name} 的 superseded-by 目标 {target} 不存在"
                superseding[name] = target
        assert "ADR-C018-r4.4-profiles.md" in superseding, \
            "C018 的独立矩阵结论已被 C023 取代，必须携带 superseded-by"

    def test_index_lists_all_adrs(self) -> None:
        path = os.path.join(_ROOT, "native", "topos_codec", "docs", "ADR-INDEX.md")
        with open(path, encoding="utf-8") as fh:
            idx = fh.read()
        adr_dir = os.path.join(_ROOT, "native", "topos_codec", "docs")
        for name in os.listdir(adr_dir):
            if name.startswith("ADR-") and name.endswith(".md") \
                    and name != "ADR-INDEX.md":
                assert name in idx, f"ADR-INDEX 缺 {name}"


class TestR7TierPixFmtDomain:
    """R7：tier_pix_fmt_choices（UI 选择域）与编码器解析表一致性。"""

    def test_all_choices_inside_encoder_domain(self) -> None:
        from src.shared.codec.topos_profiles import (
            tier_pix_fmt_choices, AVAILABLE_TOPOS_TIERS, TOPOS_PROFILE_TIERS,
        )
        from src.shared.export.topos_encoder import _parse_topos_pix_fmt

        for tier in AVAILABLE_TOPOS_TIERS:
            for with_alpha in (False, True):
                for name in tier_pix_fmt_choices(tier, with_alpha):
                    parsed = _parse_topos_pix_fmt(name)
                    assert parsed is not None, (tier, name)
                    kind, pf, bd, ad = parsed
                    assert bd in TOPOS_PROFILE_TIERS[tier].bit_depths, (tier, name)
                    if name.startswith("topos_") and re.search(r"a\d+$", name):
                        # 显式 alpha 格式契约：a{N} 后缀必须解析出 alpha 位深
                        # （'topos_cfa*le' 等裸 topos_ 前缀名非 alpha 契约）
                        assert ad is not None, name
                    else:
                        # 裸格式 = 隐式 alpha 探测（R3 语义），无 alpha 位深
                        assert ad is None, name

    def test_alpha_variant_naming_matches_planar_regex(self) -> None:
        """UI 域内 topos_* alpha 名必须同时被 timeline_export 的 planar
        识别正则接受（重导入链 P0-03 域一致）。"""
        from src.features.edit.core.timeline_export import (
            _is_topos_planar_format,
        )
        from src.shared.codec.topos_profiles import (
            tier_pix_fmt_choices, AVAILABLE_TOPOS_TIERS,
        )

        for tier in AVAILABLE_TOPOS_TIERS:
            for name in tier_pix_fmt_choices(tier, with_alpha=True):
                assert _is_topos_planar_format(name), name


class TestTrawTiersPinned:
    """traw_tiers 钉住（2026-09-13 qe 死区修复后加）。

    native manifest 的 traw_tiers（12bit/16bit 各六档）此前无任何测试
    钉住。死区事故：首版 raw16-2 锚点 qp57 由场景分歧（qe63 vs qe76）
    直接平均落进 52..63 封顶混叠段——名义 qe69 不可达（实际 63 封顶，
    51..63 字节同径）。本类把结构、跨面一致性与可分 qe 域钉死。"""

    _DOCS_MANIFEST = os.path.join(
        _ROOT, "docs", "image", "capability_manifest.json")
    _NOMINAL = ["2", "4", "6", "8", "12", "16"]

    def test_structure_and_typography(self, manifest) -> None:
        for block, prefix in (("12bit", "raw12-"), ("16bit", "raw16-")):
            entries = manifest["traw_tiers"][block]
            assert [e["id"] for e in entries] == [
                prefix + n for n in self._NOMINAL], block
            for e in entries:
                assert e["label"].endswith(e["nominal_ratio"]), e["id"]
                assert ":1" in e["nominal_ratio"], e["id"]
                # 排版规则：比率必带 :1；实测区间用 ~ 分隔（native manifest
                # 首版曾用 "1.71-2.48" 连字符无单位）
                if "measured_ratio" in e:
                    assert e["measured_ratio"].endswith(":1"), e["id"]
                    assert "~" in e["measured_ratio"], e["id"]

    def test_16bit_anchors_reachable_qe(self, manifest) -> None:
        """16-bit 锚点必须落在可分 qe 域（回归钉）。

        qe = qp_base+12（bd16 偏移）后按 qp_base 域封顶：≤63 顶格 63、
        ≥64 放开 95 → 可分 qe = [0..63]∪[76..95]，64..75 死区。
        锚点 qp 落 52..63（混叠段）即歧义值——存档语义只允许 qp51
        （+12 恰好触顶）或 v1.5 域 ≥64。"""
        for e in manifest["traw_tiers"]["16bit"]:
            qp = e["qp"]
            assert not (52 <= qp <= 63), (
                f"{e['id']} 锚点 qp{qp} 落封顶混叠段（名义 qe{qp + 12} "
                f"不可达，实际 63）——应存 qp51 或 v1.5 域 ≥64")
            if qp < 64:
                assert qp == 51, f"{e['id']} 旧域锚点应为饱和值 51，got {qp}"

    def test_native_docs_cross_consistency(self, manifest) -> None:
        with open(self._DOCS_MANIFEST, encoding="utf-8") as fh:
            docs = json.load(fh)
        for block, bd in (("12bit", 12), ("16bit", 16)):
            for e in manifest["traw_tiers"][block]:
                d = docs["tiers"][e["id"]]
                assert d["qp"] == e["qp"], e["id"]
                assert d["bit_depth"] == bd, e["id"]
                assert d["label"] == e["label"], e["id"]
                assert d["pixel_format"] == 3, e["id"]
        # 默认档两侧一致（raw12-4 / raw16-4）
        for block, did in (("12bit", "raw12-4"), ("16bit", "raw16-4")):
            nat = next(e for e in manifest["traw_tiers"][block]
                       if e["id"] == did)
            assert nat.get("default") is True, did


class TestV9MicroGop:
    """V9 zero-motion IP-2（micro-gop 计划批 5；ADR-C047）：manifest ↔ native
    ↔ binding 三方等式。"""

    def test_v9_micro_gop_matches_native(self, manifest, require_lib) -> None:
        import ctypes

        import numpy as np

        import pytest

        from src.shared.codec import topos_binding as tb

        ToposCodecError = tb.ToposCodecError
        v9 = manifest.get("v9_micro_gop")
        assert v9 is not None, "manifest 缺 v9_micro_gop 段"
        assert v9["major"] == 9 and v9["state"] == "experimental"
        assert v9["alpha"] is False and v9["scaled_decode"] is False

        codec = tb.ToposCodec()
        cfg = tb._CFrameConfig()
        cfg.struct_size = ctypes.sizeof(tb._CFrameConfig)
        cfg.abi_version = tb.TOPOS_CODEC_ABI_VERSION
        cfg.visible_width, cfg.visible_height = 64, 48
        cfg.profile, cfg.pixel_format, cfg.bit_depth = 3, 0, 10
        cfg.qmatrix_id, cfg.qp_base = 0, 40
        cfg.color_range = cfg.color_primaries = cfg.color_transfer = 1
        cfg.color_matrix = 1
        cfg.reserved[0] = int(v9["cfg_selector"])
        assert codec.config_validate(cfg) == 0, "V9 cfg 须被 native 接受"

        # GOP 编解码往返：I + 重复帧 P → 解码逐位一致（零漂锚）
        W, H, CWC = 64, 48, 32
        y = (np.indices((H, W))[1] * 7 % 1000 + 12).astype(np.uint16)
        u = np.full((H, CWC), 512, np.uint16)
        v = np.full((H, CWC), 512, np.uint16)
        d0 = [np.zeros((H, W), np.uint16), np.zeros((H, CWC), np.uint16),
              np.zeros((H, CWC), np.uint16)]
        d1 = [a.copy() for a in d0]
        with tb.ToposGopContext(codec, cfg) as ctx:
            p0, _ = ctx.encode_frame([y, u, v], force_intra=True)
            p1, _ = ctx.encode_frame([y, u, v])  # 与参考相同 → 残差 0
            assert len(p1) < len(p0)
            ctx.feed(p0, planes=d0)
            ctx.feed(p1, planes=d1)
            assert np.array_equal(d0[0], d1[0]), "零漂锚违背（重复帧 recon 不一致）"

        # P 直启拒绝（须 context 序列：首帧必须 I）
        with tb.ToposGopContext(codec, cfg) as ctx:
            with pytest.raises(ToposCodecError, match="first frame must be I"):
                ctx.feed(p1)

        # no-alpha capability：alpha cfg create → NOT_IMPLEMENTED
        bad = tb._CFrameConfig()
        ctypes.memmove(ctypes.byref(bad), ctypes.byref(cfg),
                       ctypes.sizeof(tb._CFrameConfig))
        bad.alpha_mode = 1
        bad.alpha_bit_depth = 16
        with pytest.raises(ToposCodecError, match="no-alpha"):
            tb.ToposGopContext(codec, bad)

class TestV7R3MicroGop:
    """V7-R3 zero-motion IP-2（ADR-C048 载体更换批 4）：manifest ↔ native
    ↔ binding 三方等式；与 V9 同纪律，载体 (major 7, em 8)。"""

    def test_v7r3_micro_gop_matches_native(self, manifest, require_lib) -> None:
        import ctypes

        import numpy as np

        import pytest

        from src.shared.codec import topos_binding as tb

        ToposCodecError = tb.ToposCodecError
        r3 = manifest.get("v7r3_micro_gop")
        assert r3 is not None, "manifest 缺 v7r3_micro_gop 段"
        assert r3["major"] == 7 and r3["state"] == "experimental"
        assert r3["cfg_selector"] == 11
        assert r3["alpha"] is False and r3["scaled_decode"] is False

        codec = tb.ToposCodec()
        cfg = tb._CFrameConfig()
        cfg.struct_size = ctypes.sizeof(tb._CFrameConfig)
        cfg.abi_version = tb.TOPOS_CODEC_ABI_VERSION
        cfg.visible_width, cfg.visible_height = 64, 48
        cfg.profile, cfg.pixel_format, cfg.bit_depth = 3, 0, 10
        cfg.qmatrix_id, cfg.qp_base = 0, 40
        cfg.color_range = cfg.color_primaries = cfg.color_transfer = 1
        cfg.color_matrix = 1
        cfg.reserved[0] = int(r3["cfg_selector"])
        assert codec.config_validate(cfg) == 0, "V7-R3 cfg 须被 native 接受"

        W, H, CWC = 64, 48, 32
        y = (np.indices((H, W))[1] * 7 % 1000 + 12).astype(np.uint16)
        u = np.full((H, CWC), 512, np.uint16)
        v = np.full((H, CWC), 512, np.uint16)
        d0 = [np.zeros((H, W), np.uint16), np.zeros((H, CWC), np.uint16),
              np.zeros((H, CWC), np.uint16)]
        d1 = [a.copy() for a in d0]
        with tb.ToposGopContext(codec, cfg) as ctx:
            p0, _ = ctx.encode_frame([y, u, v], force_intra=True)
            p1, _ = ctx.encode_frame([y, u, v])  # 与参考相同 → 残差 0
            assert p0[6] == 7 and p0[45] == 8, "载体字节须为 (major 7, em 8)"
            assert len(p1) < len(p0)
            ctx.feed(p0, planes=d0)
            ctx.feed(p1, planes=d1)
            assert np.array_equal(d0[0], d1[0]), "零漂锚违背"

        with tb.ToposGopContext(codec, cfg) as ctx:
            with pytest.raises(ToposCodecError, match="first frame must be I"):
                ctx.feed(p1)

        with pytest.raises(ToposCodecError, match="no-alpha"):
            bad = tb._CFrameConfig()
            ctypes.memmove(ctypes.byref(bad), ctypes.byref(cfg),
                           ctypes.sizeof(tb._CFrameConfig))
            bad.alpha_mode = 1
            bad.alpha_bit_depth = 16
            tb.ToposGopContext(codec, bad)


class TestImageNamingConsistency:
    """E4（N-7 单一注册表闭环）：TOPOS_IMAGE_TIERS ↔ image manifest ↔
    CLI kTierPresets 静态表 ↔ TMET 质量阶梯逐项一致；后续新增档位漏
    同步任一处即红。"""

    def _image_manifest(self) -> dict:
        path = os.path.join(_ROOT, "docs", "image",
                            "capability_manifest.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_image_manifest_tiers_match_registry(self) -> None:
        from src.shared.codec.topos_profiles import (
            AVAILABLE_TOPOS_IMAGE_TIERS, TOPOS_IMAGE_TIERS,
        )

        im = {k: v for k, v in self._image_manifest()["tiers"].items()
              if not k.startswith("$")}
        assert set(im) == set(TOPOS_IMAGE_TIERS), (
            "image manifest 档位集合漂移",
            set(im) ^ set(TOPOS_IMAGE_TIERS))
        assert set(im) == set(AVAILABLE_TOPOS_IMAGE_TIERS)
        for tid, entry in im.items():
            t = TOPOS_IMAGE_TIERS[tid]
            assert entry["label"] == t.label, tid
            assert entry["image_profile"] == t.image_profile, tid
            assert entry["pixel_format"] == t.pixel_format, tid
            assert entry["bit_depth"] == t.bit_depth, tid
            assert entry["qp"] == t.qp, tid
            assert entry["qmatrix"] == t.qmatrix, tid
            ma, mr = (entry.get("anchor_mb_per_frame_1080p"),
                      t.anchor_mb_per_frame_1080p)
            if ma is not None and mr is not None:
                assert abs(ma - mr) < 1e-9, (tid, "anchor_mb")

    def test_cli_tier_presets_match_registry(self) -> None:
        """image_cli kTierPresets（id/label/pf/bd/profile/qm/qp）与注册表
        逐值一致——CLI --tier 是注册表的 C 侧投影（N-7）。"""
        import re

        from src.shared.codec.topos_profiles import TOPOS_IMAGE_TIERS

        path = os.path.join(_ROOT, "native", "topos_codec", "src", "cli",
                            "image_cli.c")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"kTierPresets\[\]\s*=\s*\{(.*?)\n\};", text, re.S)
        assert m, "kTierPresets 表未找到"
        rows = re.findall(
            r"\{\s*\"([a-z0-9-]+)\",\s*\"([^\"]+)\",\s*(\d+)u,\s*(\d+)u,"
            r"\s*(\d+)u,\s*(\d+)u,\s*(\d+)u\s*\}", m.group(1))
        assert len(rows) == len(TOPOS_IMAGE_TIERS), (
            "CLI 预设数与注册表档位数不一致", len(rows), len(TOPOS_IMAGE_TIERS))
        for tid, label, pf, bd, prof, qm, qp in rows:
            t = TOPOS_IMAGE_TIERS[tid]
            assert label == t.label, tid
            assert int(pf) == t.pixel_format, tid
            assert int(bd) == t.bit_depth, tid
            assert int(prof) == t.image_profile, tid
            assert int(qm) == t.qmatrix, tid
            assert int(qp) == t.qp, tid

    def test_tmet_quality_ids_match_registry(self) -> None:
        """TMET 质量阶梯（low..ultra = 1..4）：注册表 tm_tier_id 与档位
        id 后缀逐档对应；RAW/444 家族不入阶梯（tm_tier_id=0）。"""
        from src.shared.codec.topos_meta import TOPOS_IMAGE_TIER_IDS
        from src.shared.codec.topos_profiles import TOPOS_IMAGE_TIERS

        for tid, t in TOPOS_IMAGE_TIERS.items():
            if t.tm_tier_id == 0:
                continue
            quality = tid.rsplit("-", 1)[1]
            assert quality in TOPOS_IMAGE_TIER_IDS, tid
            assert TOPOS_IMAGE_TIER_IDS[quality] == t.tm_tier_id, tid
