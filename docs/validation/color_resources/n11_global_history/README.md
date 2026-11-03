# N11 留档：全局历史统一重投影（2026-11-04）

## 实现

- **外部工程变更统一重投影**：`ColorGraphAppHost.on_external_project_change`
  ——任何路径替换 `_current_edit_project`（全局 undo/脚本/其他域命令）后，
  main_window 的 set_project 包装器把权威图新投影推给协调器（面板/资源/
  渲染随 ack 刷新）；自家命令流已推同 revision ack 时幂等跳过。
- **全局 undo/redo 图路由**：`LayerOpsMixin.undo/redo` 在图会话 READY 且
  共享工程命令栈有历史时走 `graph_host.undo/redo`（工程栈回滚 + 权威
  投影重放 + 重渲染），不再落 legacy 引擎历史（此前只有图菜单 undo
  正确，全局 Cmd+Z 会让 engine 与图权威分叉）。

## 测试（控制器套件新增 3 项，23 项全绿）

- 外部栈 undo（绕过图桥）→ last_ack 仍新 revision → on_external_project_
  change 重投影回旧 revision，面板实例随权威清空；
- revision 一致时幂等（自家命令流不重复推）；
- 图历史门控（栈空 False / 有历史 True）。
回归：app host/控制器/路由/命令 58 passed；原生验证 passed。
