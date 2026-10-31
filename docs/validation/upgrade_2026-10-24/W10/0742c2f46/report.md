# W10 · 跨机归档、文件系统与脱机往返（Windows 适配）—— 验证报告

- 基线提交：0742c2f46（W06 收口）
- 机器：B（Windows 11 Pro 26200 / NTFS）
- 对应计划：W10（R03/R13 · M20/W10）

## 交付内容（project_archive.py / consolidate.py Windows 平台适配）

### 1. 大小写冲突不再静默错连（真实缺陷修复）

`_unique_dest_name` 旧实现按**精确匹配**去重——Windows（NTFS/FAT）与
macOS（APFS 默认）文件名大小写不敏感，"A.mov" 与 "a.mov" 是同一文件：
Mac（大小写敏感盘）工程归档到 Windows 时两个"唯一"名先后 copy，第二次
**静默覆盖**第一次的归档副本，首个素材的 archived_path 指向他人内容
（跨素材错连，SHA 校验因逐素材比对 src↔dst 而双双通过，不可见）。
修复：唯一性按 `casefold()` 键判定（对大小写敏感系统只是保守改名）。
实测复现：`A.mov`(AAA) + `a.mov`(aaa) 双双完整存活、内容各归其主。

### 2. Windows 保留名/尾点尾空格净化

`_sanitize_name`（project_archive + consolidate 同契约）：保留设备名
（CON/PRN/AUX/NUL/COM1-9/LPT1-9，**带任何扩展名同样非法**）按词干匹配
前缀化（`CON.mov` → `_CON.mov`）；尾部点/空格剥离（NTFS 静默剥离导致
创建名≠请求名、校验路径错位）。真机实测 `NUL.mov` 素材落盘成功。

### 3. 可移植索引与跨平台目录哈希

- 报告 `archived_path` 统一 `/` 分隔（Mac↔Windows 报告/证据比对不把
  分隔符差异算成内容变化；relink 用平台原生绝对路径互不影响）；
- 报告固化 `hash_algo: "sha256"`（算法版本随 schema 可演进，读端按
  字段分发）；
- 目录素材聚合哈希的相对路径统一 `/` 进入哈希——**同一目录内容在
  Windows 与 macOS 得到相同哈希**（往返对拍依赖；单测以 "/" 参照锁死）。

### 4. Windows 文件锁失败恢复（复验）

锁定的源文件（PermissionError 5）按素材降级 `copy_failed` 记录、报告
仍完整产出、其余素材继续收集、`report.passed=False` 如实阻断发布判定
（AUD-17202 契约在 Windows 锁场景验证；consolidate 的输出名去重另经
`os.path.exists`（NTFS 大小写不敏感）天然防御已存在文件冲突）。

## 测试证据（tests/edit/test_project_archive_w10.py，11 绿 + 既有 9 绿）

| 验收项 | 状态 | 证据 |
|---|---|---|
| A.mov/a.mov 不覆盖/错连 | ✅ 真机 | 双源目录异 case → 双副本内容各归其主 |
| 大小写去重键 | ✅ 单测 | casefold 键 `clip.mov`/`CLIP.mov` 分离 |
| CON 等目标非法名 | ✅ 真机+单测 | `NUL.mov` 落盘成功；9 个保留名词干全净化 |
| 中文/空格名 | ✅ 单测 | CJK 保留、空白净化 |
| NFC/NFD | ✅ 单测 | 两分解形式净化键不同（不互撞不覆盖） |
| 嵌套序列（目录素材） | ✅ 单测 | 聚合哈希 = "/" 参照（跨平台同哈希） |
| 可移植索引 | ✅ 单测 | 报告无 `\`、hash_algo 固化 |
| 长路径（MAX_PATH 压力） | ✅ 真机 | ~200 字符目标树 + 110 字符名收集成功 |
| 文件锁失败恢复 | ✅ 单测 | copy_failed 降级 + 其余继续 + 报告产出 |
| consolidate 同契约 | ✅ | 保留名/尾点净化复用（14 绿回归） |

## 测试前提的 Windows 实况登记（如实）

编写对拍用例时实证：大小写不敏感文件系统上**同一目录**无法同时存在
`A.mov` 与 `a.mov`（第二次写入直接覆盖第一个——源即错）。因此冲突
用例的源文件必须分属不同目录，这正是真实场景（Mac 大小写敏感盘 →
Windows 归档）的形态；测试注释已固化该前提。

## 已知限制与如实登记

- **Mac→Windows→Mac 完整脱机往返**（卸载原盘仅靠归档包重开/输出同像素
  与 PCM、LUT/字体/外录 WAV 依赖闭包）：归档 schema/闭包由 M20 共享
  归档格式持有（Mac 集成者）；本包交付 Windows 侧文件系统适配与
  可移植性契约（分隔符/哈希/命名/大小写），往返的 Mac 端执行待 M20
  集成——Windows 侧已具备：路径重写后工程重开（既有
  test_collected_project_reopens_on_archive）+ 逐文件 SHA 校验。
- 盘符变化/UNC 路径：归档内 relink 使用绝对路径，跨机重开由
  batch_relink_assets 重写为目标机归档根（盘符变化天然兼容）；UNC
  目标未在本机实测（无网络盘）。
