# Color Gallery 后端 G01–G13 验收记录

日期：2026-11-10。执行：本仓库 `bugfix` 分支。对应计划：
`docs/plans/2026-11-09-color-gallery-backend-plan.md`。

- **批次起点**：`661b48c7`（R01–R06 收尾；上一验收
  `viewport_audit_followup_r01_r06_20261109.md`）。
- **平台**：macOS 15.3 offscreen（`QT_QPA_PLATFORM=offscreen`），
  Python 3.12.14，PyQt6 6.11.0 / Qt 6.11.2。GPU：Apple Silicon 集显
  （本验收**未**执行 GPU 呈现路径，见 §4）。
- **素材**：`tests/test_videos`（真实 DNxHR/ProRes/H.264）经
  `tests/color/test_color_real_media_smoke.py`（在场真实 FFmpeg demux）。

## 1. 提交清单

| 任务 | Commit | 内容 |
|---|---|---|
| G01 | `744cef41` | 领域合同（§4 全部 DTO＋六角色 manifest）／User-System 查询 repository／能力宿主（revision 守卫、选中项 grade 探测、more-menu 门控） |
| G02 | `7b113f58` | meta schema 2（still_id/label/album/project/timeline 快照＋未知字段保留＋原子写）；随机身份分配（不复用）；rename 首写补齐；三列 Still ID/Label/Timeline |
| G03 | `90d47c11` | rename/move/delete 统一 journal 事务（staging→installing→committed；崩溃恢复；跨卷 copy→校验→清源）；前置验证（大小写变体/孤立 sidecar/只读/symlink/realpath 归属）；删除走可恢复 trash |
| G06 | `9bfc37ac`＋`f4e437a4` | 抓帧会话（请求锁事实＋阶段复核＋取消令牌；换 clip/帧→cancelled）；窗口级 coordinator（shutdown 3.8 步）；完成刷新不抢回已切走的 album |
| G07 | `0619d719` | timeline 全量应用统一 `_paste_grade_to_clip`（单 undo，工程一致）；目标拒绝不回落引擎层；两 handler 视频恢复换 generation 守卫 |
| G04 | `c5a59620` | scope 规格落码（project 无存储不伪造）；PowerGrade 真实文件夹存储（显式建/可回退）；虚拟 Timelines（timeline_id 键控、offline 标记、只读） |
| G05 | `8d1e21d0` | mtime 索引查询（未变目录零磁盘；搜索/排序内存重放）；代际守卫后台 runner；面板扫描走索引 |
| G08 | `ef933542` | `.toposbundle` 导出（manifest+sha256+精度/alpha/LUT 引用计数；源不动）；导入安全线（数量/体积/穿越/symlink/未来 schema/hash 篡改）；身份冲突政策（重复拒绝/换发＋origin 保留） |
| G09 | `16938a5b` | still_precision 只按 EXR 实际成功记录；四通道参考 alpha 合同（undeclared 拒绝；对齐 premultiplied）；超 1.0/负值不 clamp 钉死 |
| G10 | `546cc415` | 旧脚本格式可恢复迁移（journal-first、mapping 幂等、不删源、未知字段保留）；脚本 capture/apply/list 与 Gallery 同一资源；`migrate_legacy_presets()` |
| G11 | `d39bb2d2`＋`8990fd01`＋`a0e7dee9` | 版本化显示偏好（原子落盘、损坏/未来 schema 回退不清文件、legacy 列数→宽度）；mtime 门控的可见性同步；重扫保留在场选中 |
| G13 | `b7ab0bf4` | wipe 参考会话 provenance（still 身份/revision/实际精度；失败保留旧会话；清除即结束）；split 协议如实拒绝（presenter 未实现不假启用） |
| G12 | 本提交 | 验收记录＋计划表收尾 |

## 2. 测试证据

```
# G12 验收矩阵（Gallery 全链 ＋ 计划 §5 G12 表列出的既有套件）
QT_QPA_PLATFORM=offscreen pytest -q --disable-warnings \
  {G01–G13 十五个新套件} tests/color/test_presets_gallery.py \
  tests/ui/test_column_grid_panels.py tests/project/test_save_router.py \
  tests/color/test_ab_reference_wipe.py \
  tests/color/test_color_tools_scripting_api.py \
  tests/scripting/test_script_api_versioning.py \
  tests/color/test_color_real_media_smoke.py
→ 393 passed / 0 failed / 3 skipped（真机素材在场真实解码；其余为
   case-sensitive FS / 权限语义 skip）

# 跨域回归锚点（R06 记录的 67 套件清单）
pytest -q $(cat docs/validation/viewport_second_review_20261109_tests.txt)
→ 1286 passed（含随 G03 事务化更新的 wd4g23 rename 回滚用例，
   验收口径不变：注入 sidecar 安装失败 → 弹窗报告＋旧集合原样）

# 原生启动（隔离配置）
TOPOS_CONFIG_PATH=/tmp/... TOPOS_TEST_MODE=1 \
  .venv/bin/python -m src.main --product studio --test-startup \
  --no-startup-dialog → exit 0，无 traceback
```

新用例分布（15 个新套件，共 **199 个新测试**）：
G01 contracts 19／repository 20／能力宿主 18；G02 meta schema 17；
G03 bundle 事务 24；G06 抓帧会话 14；G07 应用目标 9；G04 集合 scope 14；
G05 查询服务 11；G08 传输 13；G09 HDR provenance 9；G10 旧库迁移 14；
G11 偏好 9；G13 比较会话 8。过程自伤均随批修复（见各提交说明：
coordinator 锁重入死锁、偏好默认值改动旧行为、scan 中 folder 未赋值
即用等，均有钉死用例）。

## 3. 大库查询基线（offscreen CPU 代理实测）

合成平铺库（每条＝真实最小 .topos zip，无 meta）：

| 库规模 | 首次查询（miss，含建索引） | 索引命中查询 | 搜索 p95（命中路径） |
|---|---|---|---|
| 1,000 条 | 77.0 ms | 0.97 ms | 1.05 ms |
| 10,000 条 | 684.9 ms | 23.3 ms | 16.15 ms |

- 首次查询后同目录重扫/搜索零磁盘（stats：hits≫misses=1）；
  rename/delete/capture/导入都改目录 mtime 自动失效。
- 10k 档命中查询 23ms：meta 缺失走 Python 逐条 `matches_query` 的
  内存重放，UI 主线程单次可承受但不达标到"每帧"——真实大库 UI
  （分页/可见区驱动）留给 G05 后续接线（见 §4）。

## 4. 真机证据边界（如实未执行，不填通过）

- **GPU 呈现路径**：分屏比较 presenter（真实渲染队列消费者、VRAM 预
  算）未实施——G13 的 split 入口如实拒绝（`split_requested` 闭环），
  Start Comparison 保持禁用；`ColorCompareSession` 仅承接 wipe 会话
  provenance。
- **滚动帧率／10 分钟内存／双视图**：offscreen 无 GPU 交换链，未测。
- **PowerGrade/Timelines 的 UI 导航**：存储与虚拟集合服务真实
  （G04），chrome 侧入口仍禁用（capability `collection_service_missing`）
  ——启用需要布局/导航接线（G11 chrome 部分因面板拆分 WIP 未动）。
- **跨机器导入**：`.toposbundle` 跨目录往返已测；真实跨机（不同用户
  目录/盘符）未执行。
- **DRX/DPX 互操作**：按计划明确禁用，未实现。

## 5. 已知未实施产品范围（维持计划 §6 口径）

Memories（无模型）、project-scope album（无存储服务）、DRX 兼容、
分屏 presenter。所有 UI 禁用项带稳定 reason；enabled 动作均有真实
handler 与结果回填。
