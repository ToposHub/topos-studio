# T01 · 只读洞察快照与宿主接口契约（v1）

> 状态：**API 已实现、契约测试已验（21 绿）**；真实宿主场景（G1）待 T02/T21。
> 代码：`src/features/edit/insights/`；测试：`tests/edit/insights/test_t01_*.py`。

## 1. InsightSnapshot v1（dto.py）

```
InsightSnapshot
├─ schema_version: "1"（固定）
├─ capture_id: str（uuid；不参与 content_hash）
├─ captured_at: float（捕获时间；不参与 content_hash）
├─ project: ProjectEntry{project_id, name, revision}
├─ sequences: (SequenceEntry…)
│   ├─ sequence_id/name/fps:FpsSpec{num,den,drop_frame}/width/height/sample_rate
│   ├─ revision（乐观锁计数；不参与 content_hash）
│   ├─ load_state: "loaded" | "unloaded"（ref 在而序列未物化=unloaded，
│   │   duration_frames=None=未收集，不冒充 0）
│   ├─ tracks: (TrackEntry{track_id,name,track_type,index,is_enabled,
│   │   track_role, clips:(ClipEntry…), cues:(CueEntry…)})
│   │   ├─ ClipEntry{clip_id/sequence_id/track_id/track_type/asset_id,
│   │   │   timeline_[start,end) 半开, source_[start,end) 半开, is_enabled,
│   │   │   source_mapping, reverse, constant_speed, linked_clip_id, label_color}
│   │   └─ CueEntry{clip_id, sequence_id, track_id, text, timeline_[start,end)}
│   └─ markers: (MarkerEntry{marker_id, frame, name, color, comment, kind, …})
├─ assets: (AssetEntry{asset_id, display_name, media_type, is_offline,
│   file_basename(仅 basename 脱敏), reel/scene/shot/take,
│   keywords/rating/flag/color_label/comment})
├─ payload_hash: 完整规范载荷 sha256（除 hash 字段本身；含 capture_id/时间/revision）
└─ content_hash: 逻辑内容 sha256（剥离 capture_id/captured_at/project.revision/
    每 sequence.revision；T15 判断"编辑内容是否真的变化"用）
```

`source_mapping` 口径：`identity`（恒速 1.0 正放、无冻结）｜`constant`
（恒定倍率，附 constant_speed）｜`unknown`（速度点/曲线域/反向/冻结——
M04 未验收，工具不得自行推导逐帧映射）。

## 2. capture（capture.py）

`capture_readonly_state(state, *, capture_id=None, captured_at=None) -> InsightSnapshot`

- 输入为 `ProjectSequences` 形态（`.project` + `.sequences` dict），即宿主
  `get_project_sequences()` 的返回形态；
- 白名单深复制（构造式，参考 snapshot_service 纪律）：捕获后宿主 dict/
  metadata 继续变化不影响 DTO（有回归测试）；
- 只读：不 bump revision、不改宿主 dict 键集、不触文件系统；
- 结构错误（缺 project/sequences）抛 ValueError；"无工程"是宿主层
  CaptureResult 的 `NO_PROJECT`（host_protocol）。

## 3. codec（codec.py）——唯一 snapshot codec（T14 复用、T15 保存/读取）

- `serialize_snapshot` → 规范 JSON（排序键、ensure_ascii=False）；确定性输出；
- `parse_snapshot` → `SnapshotParseResult{ok, snapshot, error{code,message,path}}`，
  错误码：`INVALID_JSON` / `UNSUPPORTED_SCHEMA`（v1 严格） / `UNKNOWN_FIELD`
  （v1 严格拒绝未知键）/ `MISSING_FIELD` / `INVALID_VALUE` /
  `INVALID_TIMEBASE`（半开区间 end≥start≥0、fps num/den>0、bool≠int）/
  `HASH_MISMATCH`（重算 payload hash 比对）；
- 同一 snapshot 序列化→重读恒等（dataclass 相等，有测试）；
- unloaded 序列 duration_frames 必须为 null、loaded 必须收集（parse 校验）。

## 4. host_protocol（三个拟新增协议）

| 协议 | 签名 | 语义 |
|---|---|---|
| 捕获 | `capture_readonly_state() -> CaptureResult{ok, snapshot, error_code, message}` | 同一时点 project+完整序列；error_code ∈ no_project / inconsistent_state / capture_failed |
| 导航 | `navigate_to(NavigateRequest) -> NavigateResult` | UI 线程调用；Request 带 capture_id/project_id/sequence_id/entity_id/target_frame/expected_sequence_revision；Result 带 status(navigated/rejected)+reason(no_project/capture_stale/project_changed/sequence_changed/target_missing/not_supported)+actual_frame |
| 缩略图 | `request_thumbnail(ThumbnailRequest) -> ThumbnailResult` | Request 带 capture_id/asset_id/source_frame/尺寸/源指纹；Result quality ∈ source_thumbnail/placeholder/unavailable（源缩略图≠最终画面） |

G1 落地时宿主实现 `InsightsHostProtocol`（runtime_checkable Protocol）。

## 5. 复用/不重做声明

- fps/DF 复用 `src.shared.media.timecode.Timecode`（Rational + auto_detect_type）；
- 路径脱敏沿用 `snapshot_service._redact_path` 纪律（仅 basename）；
- 直接使用统计语义与 `asset_usage_index` 一致（timeline_start+duration 半开）。

## 6. 已验 / 未验

- 已验（focused 测试，`.venv/bin/python -m pytest tests/edit/insights/ -q` →
  21 passed, exit 0）：深冻结、只读性、unloaded 语义、半开区间、NTSC DF
  有理 fps、hash 双轨（revision/捕获元数据不入 content_hash）、codec 全部
  错误码、序列化恒等、中文/引号/多行文本 roundtrip。
- 未验（待后续包）：真实宿主捕获（G1b）、缩略图指纹实际供应（T13）、
  10 万实体规模性能（T20）。
