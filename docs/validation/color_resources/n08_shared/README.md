# N08 留档：Shared 跨 clip（2026-11-04）

## 实现

- **原子 fanout**：`GraphProjectBridge.update_shared_definition` 改为
  ProjectMacroCommand——所有引用栈**一个宏 command**（此前每栈一命令，
  undo 需多次）；宏失败整批不落地；**锁定的引用栈显式缺席**（后端校验
  域，fail-visible 不误写）。
- Make Local（既有）复核：深复制定义参数为本地、解除引用、保留当时值，
  不解除用户锁。

## 测试（tests/app/test_shared_fanout.py，3 项）

- A/B 栈同 id 定义 → fanout 两栈更新 + **一次 undo 全还原**；
- 锁定栈缺席（updated 仅含未锁栈）；
- Make Local 保留当时 grade 参数、节点回 Corrector。
回归：compound_shared/commands/fanout 28 passed；原生验证 passed。

## 剩余（如实登记）

跨工程粘贴共享定义的冲突重命名/去重规则与剪贴板共享闭包随 N12（选择性
粘贴域）批次认证——当前跨工程粘贴走节点载荷通道（B14），定义 id 冲突
表现为各自独立定义（不覆盖、不串改——安全但不共享）。
