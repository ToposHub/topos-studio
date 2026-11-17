# Files 插件完成情况复核记录

2026-11-17，基线 `c76724e3`。PyQt6 + ModernGL + GLSL 项目；本轮修改不触及 GPU 渲染路径。

## 验证

- 修改前 focused：插件共享层、Files catalog/启动/声明与原生插件 UI，**294 passed**。
- 修改后完整范围：**697 passed，0 failed，0 skipped，35.71s**，其中新增回归 47 项。
- 最终独立 Review：启动服务 15、声明 manifest 11、原生插件 UI 18，共 **44 passed**。
- `git diff --check` 通过。

完整范围包含 `tests/plugins`、`tests/files`、`tests/ui/test_native_files_*.py`，以及 Files 原生宿主/位置/会话/画廊四布局/图片工厂关闭、共享播放语义、音频、YUV 呈现、导入补偿、Files 架构与偏好设置用例。播放用例沿用用户提供的视频目录并开启 `tests.real_media.STRICT`；测试写入仅使用临时夹具。

原始 JUnit 报告保留在本机临时目录；提交只保存不含素材路径的汇总，避免把机器路径和媒体参数名带进新的报告。

## 仍未通过的结构性不变量

本轮以纯内存和 TemporaryDirectory 夹具复现，见 [结果](remaining_reproductions.json)：

1. 未知 manifest 字段经 parse → to_dict → parse 后丢失。
2. 修改输入的嵌套 schema 字典会改变解析后的 manifest。
3. schema 999 没有被兼容校验拒绝。
4. 两个贡献句柄中后者 release 抛错，前者未被尝试释放。
5. 停用插件 A 会取消同一 controller 下插件 B 的任务。
6. 同 id 两个清单记录没有产生重复身份诊断。
7. 安装目标 versions 指向临时根之外时，安装写到了链接目标。
8. 从预发布升级到正式版本被误判为 VERSION_NOT_NEWER。

另外通过读线程屏障验证 snapshot 可混合旧 revision 与新 records。以上均写入[新计划](../../plans/2026-11-17-files-plugin-completion-review-plan.md)，没有标成已修复，也没有将这些故障例混入通过数量。

## 提交隐私范围

只提交本轮修改的 Python 源码、测试、Markdown 与布尔结果 JSON。没有新增图片、视频、音频、缩略图、私人截图或原始媒体清单；原始素材与本机截图保持在原位置。现有 `.gitignore` 的 Files 私人验证截图/清单规则继续有效。
