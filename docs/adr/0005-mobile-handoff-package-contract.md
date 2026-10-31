# ADR 0005 — Topos Edit 移动版交接包契约：版本冻结 + 资源协议 + 规范哈希

- **Status:** Accepted（D0 契约冻结；落地批次 D1-D3 见计划表 DESK-01～08）
- **Date:** 2026-10-25
- **Plan ref:** `topos-color-mobile/docs/plans/topos_edit_mobile/04_desktop_coordination_plan.md`（DESK-01A / DESK-01B / DESK-01D / DESK-05A / DESK-08A）
- **Implementation:** `src/shared/project/package_spec.py`（冻结规范 + 校验器 + 哈希）、`src/shared/contracts/`（跨端时间精度向量）、`tests/project/test_package_contract.py`（合同测试与金丝雀）
- **Supersedes:** `src/shared/project/schema_v1_spec.py` 的"规范"地位（该文件保留为 v1 历史参考，不再是契约来源；其 manifest `additionalProperties: False` 与 `single_asset` 枚举已与生产 serializer 脱节）
- **复核触发:** 任何一方（桌面 Python / 移动 Dart）需要改包字段、版本常量、资源 URI 文法或时间精度语义时

## Context

Topos Edit 移动版要与桌面版交换**单个自包含 `.topos` 包**并双向继续精剪
（手机生成 → 桌面打开精剪 → 另存 → 手机再打开）。交付前提是双方对
"包里有什么、字段长什么样、版本怎么演进、内嵌资源怎么引用、时间怎么
计算"有**同一份**契约。此前的事实：

1. 包 schema（v3）与编辑内容 schema（v4）分散在 `manifest.py` 与
   `project_migration.py`，无一处声明"最小可读版本"与"更高版本的处置"。
2. `schema_v1_spec.py` 是 v1 时代手写 JSON Schema，早已与生产 serializer
   不一致（枚举含已废弃 `single_asset`、manifest 关闭未知键），且
   `validate_json` 依赖 venv 未安装的 jsonschema（fail-closed），不能当
   交接运行依赖。
3. 素材只按 `path_relative` 相对包目录解析：原片塞进 ZIP 不会被解码器
   找到；字体/LUT/生成资源没有内容身份，换机即断链。
4. 时间语义（有理数 FPS/PTS/帧相位/half-up/样本偏移）在桌面侧已有
   Fraction 实现与 oracle 测试，但没有可供 Dart 侧逐点对拍的**数据向量**。

## Decision

### 1. 版本冻结与分级（`package_spec.py`）

| 维度 | 当前版本 | 最小可读 | 高于当前时的处置 |
|---|---|---|---|
| 包 schema（manifest.schema_version） | 3 | 1 | future：尽力兼容打开；**保存绝不降级**（`ToposProjectIO.save` 既有 never-downgrade 守卫） |
| 编辑内容 schema（edit_content_schema_version） | 4 | 1 | future：不盲迁移（`migrate_project_payload` 既有行为）；legacy：走 `CONTENT_MIGRATIONS` 链升级 |
| 资源索引 schema（resources.json.schema_version） | 1（本 ADR 新冻结） | 1 | future：解析器按已知字段尽力读取，未知条目原样保留 |

`classify_package_versions()` 输出 current/legacy/future/unreadable 四级，
供 DESK-01D 的加载能力预检消费；`unreadable`（低于最小可读）必须受控
拒绝打开，不允许静默误读。

### 2. 字段 schema：声明式冻结 + 未知键放行

- 契约唯一来源：`src/shared/project/package_spec.py::PACKAGE_FIELD_SCHEMAS`
  （JSON Schema 子集：type/required/properties/items/enum + 本地扩展
  `value_schema` 约束 map 条目）。**永不使用 additionalProperties:false**
  ——未知字段保留（DESK-04A）本身是契约。
- 自带最小校验器 `validate_payload`：不依赖 jsonschema；Dart 侧实现同
  一子集（object/array/string/boolean/null/integer/number、enum 精确
  成员判定、bool 不算 integer/number）即可得到相同判定。
- 生产 serializer 一致性由合同测试强制：黄金包（生产 `ToposProjectIO.save`
  真实写出）逐条目过 `validate_payload`，schema 漂移即测试失败。

### 3. 资源协议（DESK-01B 实现的契约面）

- 包内新增 `resources/resources.json`（schema v1）+ `resources/` 二进制
  目录；每个资源按 **SHA-256 内容寻址**登记（resource_id / kind /
  archive_path / size_bytes / sha256 / original_filename / mime / roles /
  可选 asset_id / metadata）。
- 逻辑 URI：`topos-res://<resource_id>` 与内容别名
  `topos-res://sha256-<64hex>`；文法校验 `is_valid_resource_uri`。
  素材/字体/LUT 等引用方一律持逻辑 URI（或 content id），**不持本机
  缓存路径**——缓存路径是加载机器的本地实现细节（DESK-03C）。
- `archive_path` 必须位于 `resources/` 前缀下，路径穿越拒绝；解包/提取
  遵循既有 zip 防护（成员数/总字节数上限、symlink 拒绝）。
- 无资源索引的旧包是合法状态（resource_index 判 legacy-0），解析层按
  需回退外部路径。

### 4. 规范哈希（"双方使用同一规范哈希"）

`spec_digest()` = SHA-256( canonical_json(SPEC_IDENTITY) )，其中
canonical JSON = 键排序 + 无空白 + ensure_ascii=False + 不允许 NaN。
身份块含 spec 修订号、三个版本常量、最小可读、URI 文法、全部字段
schema。双端各自计算比对；桌面侧由合同测试钉住金丝雀值，**任何契约
变更必须显式更新该值**（评审以哈希 diff 为准）。

### 5. 跨端时间精度向量（DESK-05A）

- `src/shared/contracts/timebase.py` 以**独立 Fraction oracle**（不复用
  生产代码）生成 `data/timebase_vectors_v1.json`：恒等无漂移、混帧率
  CFR（half-up，含精确 .5 界值）、变速/倒放、非零入点+相位、连续分割
  采样分区、VFR（dyadic PTS，tie 归早帧）、样本偏移（subframe 分解/
  合成互逆、声明率→参考率残差记账）。
- 合同测试三层：(a) 提交的 JSON 向量与生成器再产物逐字节一致（文件
  新鲜度）；(b) 独立 oracle 重算每个期望值（生成器自身不失真）；
  (c) 桌面生产实现（`map_frame`/`TimelineSampleClock`/
  `split_sample_partition`/`subframe_decompose`）逐点对拍向量。
- 已知实现近似（如实记录，不掩盖）：生产 VFR 路径对目标时间取 float
  后 bisect；契约是**精确有理数最近邻、距离平局归早帧**。向量点全部
  选在 float 舍入不改变判定的位置（生成器内置 float/rational 双算
  一致性断言），Dart 侧应按精确有理数实现。
- `edit/timeline_sequences.json` 的 fps 序列化为有理数字符串
  `"<num>/<den>"`（现状冻结）；任何一端不得退化为 float/毫秒。

### 6. 黄金样例与合同测试（DESK-08A 基础）

- 黄金包由测试内确定性构建（固定 ID/时间戳，经**生产** save 路径落盘），
  断言：结构校验全过、版本等于冻结常量、另存往返逐条目等价、未知键
  （manifest/project 层）load→save 存活。
- 黄金包是结构黄金（逐条目 canonical JSON 比较），不是字节黄金——ZIP
  时间戳不参与契约。

## Consequences

- 正向：双端有可机器比对的契约面（哈希 + 向量 + 黄金包）；schema 漂移
  在桌面侧 CI 即失败，不必等联调。
- 负向/成本：字段演进流程变重（bump 版本 + 迁移 + schema + 金丝雀 +
  黄金包五处同步）；这是刻意的——交接格式的事故成本远高于流程成本。
- `schema_v1_spec.py` 保留但降级为历史参考；新代码一律引用
  `package_spec`（后续批次逐步迁移既有 import）。
- 移动端 Dart 侧需实现：canonical JSON、schema 子集校验、SHA-256 内容
  寻址、`topos-res://` 解析、时间向量的精确有理数实现。均无平台障碍。
