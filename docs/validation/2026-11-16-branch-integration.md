# 分支整合与本机认证记录

日期：2026-11-16。仓库：ToposHub/topos-studio。

## 分支结论

| 分支 | 整合前 | 整合后 / 处理 |
| --- | --- | --- |
| main / origin/main | d7c564db | 已快进至 f0c6d780 并成功推送 GitHub |
| origin/bugfix | f0c6d780 | 已全部包含于 main，无需重复合并 |
| origin/fix-ui | f0c6d780 | 与 bugfix 完全相同；该提交已合并两路更新 |
| origin/dev | d7c564db | 是 main 的祖先，无独有提交；本轮未改动远端 dev |
| codex/files-native-library | 基于 f0c6d780 新建 | 后续 Files 原生素材库开发分支 |
| codex/backup-main-before-integration-20261116 | d7c564db | 本地保留整合前 main 指针 |

main 是最新整合提交的祖先，快进包含58个新增可达提交；无需解决合并冲突。涉及音频、Deliver、Color及公共UI等267个文件。现有Files修改和本地src/main.py修改不与上游改动重叠。

## 本地内容保护

整合前备份目录：`/Users/hengshi/.codex/backups/topos-studio/files-wip-20261116-Qm5QGE`。

- `tracked.patch`：Files宿主与用户原有src/main.py的未提交修改。
- `native-files-untracked.tar.gz`：新增Files界面、transport、测试、计划与截图。
- `status.txt`、`original-head.txt`：整合前状态和HEAD。

用户原有 `src/main.py` 的 macOS 启动环境修复与 `.venv-oldmac-backup/` 保持原样，不纳入本次Files提交。

## 验证

- 基于最新main重跑Files专项与相关旧回归：47 passed，42 deselected。
- 上游新增/改动模块的选定回归：87 passed，1 skipped；覆盖扩展调色参数、Deliver布局、时间线分隔、公共分隔控件和音频参数校验。
- `git diff --check` 通过。
- 推送后重新读取远端heads：main、bugfix、fix-ui均为f0c6d780；dev仍为d7c564db。

这是针对性整合验证，不代表重跑整个项目测试套件。

## GitHub绑定

用户完成官方设备网页授权后，GitHub CLI以ToposHub登录，凭据保存于系统钥匙串；已配置HTTPS Git认证，并验证仓库管理权限和main推送成功。

聊天中提供的个人访问令牌未用于登录，未写入仓库、配置或脚本。建议用户撤销该已公开于聊天的旧令牌。后续通过已建立的网页登录凭据访问。
