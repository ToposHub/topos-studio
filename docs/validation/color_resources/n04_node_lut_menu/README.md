# N04 留档：节点 LUT 菜单（2026-11-04）

- 菜单启用：No LUT（remove_lut）/ Browse…（QFileDialog → R02 会话导入
  [验证/注册/暂存] → set_lut）/ 已注册资源条目（会话目录前 12 项）；
  勾选态读权威 `params.resources.lut_ref`（menu_options 投影）。
- 与浏览器共用命令路径与 topos-res 选择（菜单 Browse 走同一条
  import→activate_resource 链）；main_window 处理器经正式控制器。
- 测试：菜单信号/权威勾选投影/移除路径（控制器套件 20 项）+ 菜单对照
  更新（LUT 启用，Color Space/Gamma/Node Cache 仍禁用——N05/N06 前保持）；
  原生验证 passed。
