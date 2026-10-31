# AI 自主运行可靠性批次报告（2026-08-09）

本批围绕 P3/P6/P7 完成了一组不依赖新增第三方密钥的生产可靠性补强。

## 已落地

- `AgentLoop` 支持显式 `resume=True`；恢复前校验 project/timeline/base revision/素材/证据范围哈希和重新规划后的 plan hash，范围或计划变化时安全停在 `WAITING_FOR_USER`。
- AgentLoop 增加宿主拥有的补偿回滚边界；部分动作失败、验证失败或异常时只回滚已成功动作，回滚失败会作为显式诊断返回。
- `AgentStep.policy_request` 把资产、费用、网络 host、文件扩展名、模板和风险标记传入 `ExecutionPolicy`，动作级策略不再只依赖 tool name。
- `JobRecoveryManager` 只恢复明确标记 `resumable=true` 的 provider checkpoint，带 provider/job scope、父 job、恢复次数和单次尝试上限；不可恢复任务明确记录 skipped/blocked。
- `JobRegistry.cleanup_terminal` 增加年龄保留门，`prune_events` 限制事件内存增长；活动 job 不会被清理。
- provider-neutral HTTP generation checkpoint 明确声明 `resumable=true`，可被受控恢复入口识别。

## 验收

- 针对性测试：20 passed
- 自主 Agent、TaskGraph、SkillRuntime、事务、工作流、组合根回归：142 passed
- 未触碰真实时间线写入；恢复仍必须经过宿主的 provider/request scope 校验和既有审批边界。

## 仍待完成

本批没有伪造真实媒体 provider 的凭据。P2 的真实图片/视频/音乐/TTS provider 仍需用户实际授权和区域/版权条款验证；P3/P6 还需把本批 rollback/recovery 入口接到所有宿主 controller/skill/composite action；P8/P10/P11 仍需宿主导出完成回调、全素材并发/故障报告和真实 Alpha/Beta 运营演练。
