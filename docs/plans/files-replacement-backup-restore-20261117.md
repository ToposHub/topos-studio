# Files 新系统备份/恢复与开发数据重置设计（A 阶段交付）

日期：2026-11-17。基线 `a36b96cf`。本文是[总计划](2026-11-17-files-final-replacement-and-upgrade-plan.md) A 阶段第 5 项交付物（设计）；命令实现、隔离样本演练与崩溃/磁盘满注入在 H 阶段执行。范围：**只为新系统自身的数据提供备份/恢复**；不迁移旧 JSON 元数据导出（矩阵 §6.3）。

## 1. 备份什么 / 不备份什么

| 内容 | 备份 | 说明 |
| --- | --- | --- |
| `catalog.db`（schema_version、全部表、job_log 未完成项） | ✅ | SQLite 在线备份 API（`sqlite3.Connection.backup`）取一致性快照，先 WAL checkpoint |
| `libraries.json`、`schema_version` | ✅ | — |
| `plugins/state.json` + `plugins/install/` 清单（不含包体） | ✅ | 只记 id/版本/来源摘要；包体可重装，不进备份 |
| `trash/<library_id>/` 文件体 + `trash_items` 清单 | ✅ | 回收站是"可恢复数据"不是缓存 |
| `cache/`（thumbnails/strips/probe） | ❌ | 可再生，备份单列排除 |
| 素材源文件、`.xmp` 旁车 | ❌（不复制、不修改） | 备份只含**引用**（库根 + 相对身份 + 摘要），恢复时校验引用存在性 |
| 秘密/凭据 | ❌ | 按自身政策另存；备份 manifest 不含 token/完整私人路径（矩阵 §5 隐私规则） |

## 2. 备份格式与命令

命令（开发/维护 CLI，非 GUI）：`topos-files backup create|list|restore|verify`。

```
<config_root>/files-data/backups/
└── 20261117-153000-<uuid4前8>/
    ├── manifest.json      # 版本 1：格式版本、创建时间、库注册表、表行数、
    │                      # 每文件 sha256+size、素材引用摘要（不含绝对路径明文，
    │                      # 用 库id+相对路径 哈希）、插件清单
    ├── catalog.db         # 一致快照
    ├── libraries.json、state.json
    └── trash/<library_id>/…
```

**创建**：staging 临时目录全量写完 → 逐文件 sha256 记入 manifest → `os.replace` 原子落位到最终名；任何一步失败整目录作废并清理，不留半备份。**不覆盖既有可用备份**；保留策略默认最近 5 份（可配置），淘汰最旧，淘汰失败只是警告。

**verify**：重算全部 sha256 与行数对照 manifest；输出逐项布尔结果，供 H 演练与日常校验。

**restore**：`--into <空目录>`（必须为空或不存在；默认拒绝覆盖非空目录，`--force` 也只接受"先完整搬家旧目录再落位"，不做原地合并）。步骤：校验 manifest → 恢复文件 → 打开 catalog.db 比对 schema_version 与行数 → 输出核对报告（记录数、库清单、引用存在比例——失联引用列出且不阻断，恢复后由 B 的重链/missing 机制处理）。**恢复不写素材卷**。

**崩溃/磁盘满语义**（H 注入验收）：
- 创建中断 → staging 清理或整目录作废；`backups/` 内不存在半成品目录名。
- 恢复中断 → 目标目录保留已写入部分 + `.restore-incomplete` 标记文件；重复恢复要求先清目标；永不碰源 `files-data/` 与既有备份。
- 损坏 manifest/缺实体 → verify/restore 明确列出缺失项并以失败退出，不静默跳过。

**一致性点**：备份期间冻结组织数据写入（B 阶段的库级写锁/暂停作业提交；素材文件本身不动）。

## 3. 开发数据重置（与备份严格分离）

`topos-files dev-reset --list/--apply --yes`：仅处理[数据边界文档](files-replacement-data-boundary-20261117.md) §5 受管清单；`--list` 打印逐项路径/体积/性质（库数据｜可再生缓存｜回收站），`--apply` 按清单逐项执行并输出前后摘要核对；**拒绝**符号链接目标、拒绝清单外路径、拒绝任何仓库外路径；仓库根 `topos.db` 含宿主 settings，重置前先自动做一次 §2 备份再清空（防误删唯一开发库）。源素材目录与其摘要不在清单内——"重置后源素材摘要不变"是 H 的验收断言。

## 4. 隔离样本（H 演练用，A 阶段定义）

合成库样本：2 个库根 ×（若干 PNG/JPEG/TXT + 子目录 + 同名不同目录文件 + NFC/NFD 变体名）+ 预置（评分/颜色/标签/层级标签/集合/堆叠 schema 预留表行/回收站 1 项/job_log 1 条未完成项）。演练断言：空库→写入→备份→清空→恢复→记录数/关系/摘要一致；重复执行幂等；含回收站实体与清单的恢复可再恢复/清理。全部临时目录夹具，无真实素材。

## 5. 阶段归属

- A（本文件）：设计冻结。实现细节偏离须先改本文。
- B：job_log/事务使"一致性点"可行；trash_items 版本化清单。
- H：CLI 实现 + §4 样本演练 + 崩溃/磁盘满/损坏注入 + NFC/NFD/大小写/盘符/UNC 与失联重链测试（总计划 H 表）。
