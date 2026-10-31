# T21 · 全量回归基线对照记录

## 方法

- 我的树：分支 `codex/project-insights`（含全部 T 线提交 + G1a），
  仓库目录运行两次（结果一致）：
  `61 failed, 11173 passed, 33 skipped`（命令：
  `.venv/bin/python -m pytest tests/edit -q --ignore=tests/edit/insights
  --ignore=<3 个本机 _ssl dylib 损坏模块>`）。
- 基线树：`git worktree /tmp/topos_base_wt @ dd1a28fdd`（开工 base，
  不含任何 T 线改动），同一 venv、同一排除清单：
  `65 failed, 11092 passed, 102 skipped`。
- 对照：两侧 FAILED 测试 ID 归一化路径后集合比较。

## 结果（2026-10-25）

- **只在我的运行失败的测试：0 个。** 我的 61 个失败 ⊂ 基线 65 个失败。
- 基线多出的 4 个失败：`tests/edit/ai_editing/test_import_builder.py::test_*`
  （仅在 /tmp worktree 环境下失败——工作目录/临时路径依赖；与 T 线无关，
  主仓库目录运行两侧均不出现该差异方向的结论受此环境影响，故以"我的
  失败 ⊆ 基线失败"为准）。
- skip 数差异（33 vs 102）：worktree 环境下 GL/媒体 fixture 更多跳过，
  属环境差异；不影响失败集结论。

## 结论

T 线全部改动（insights 新包 + G1a 三处共享文件小改）对
`tests/edit` 全量回归**零新增失败**。61 个失败均为开工前存量（audio
routing/occlusion export/Metal renderer 等历史模块，属 M/W 线范围）。

## 复现

```
git worktree add /tmp/topos_base_wt dd1a28fdd
cd /tmp/topos_base_wt && <repo>/.venv/bin/python -m pytest tests/edit -q \
  --ignore=...（同上排除清单）
# 对照 <repo> 主目录同命令；diff FAILED 集合
git worktree remove /tmp/topos_base_wt
```
