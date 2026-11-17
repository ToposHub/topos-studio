# F 阶段第十一批（F11）：安装/更新/卸载接生命周期与 Inventory

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/package_lifecycle.py`
（新）+ `files_panel` 生产组装。复用 LocalPackageInstaller（PL-08 事务）、
PluginHostSession（F5/F6 停用链路）、DesiredStateStore（F2）。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `package_lifecycle.py`（新） | **`PackageLifecycle`** 编排：① **停用→安装→重扫**——更新/卸载前走停用链路（停新→按 owner 取消→确认终止→清理贡献）；未完全停稳中止 `PLUGIN_BUSY`（如实报告残留、可重试），绝不带着活插件换版本；安装/卸载后触发 `refresh_inventory`（F1 重扫钩子）。② **新增权限重确认**——对比已安装版本 manifest 的 `permissions`（unknown_fields/metadata 原样保留）；更新出现**新增项**必须经确认回调放行，**回调缺位同样拒绝**（不因宿主未接线而静默放行）；纯移除不触发；**首装无基线**（安装同意即权限同意，不构成"新增"）。③ **卸载三类资产分开**——代码（安装根树）经 installer 事务；**用户数据**（配置命名空间）仅 `remove_data` 时清；**凭据**（宿主 secret 授权）总是撤除；desired 偏好随代码清；**素材不触及**（编排层不碰媒体路径，测试以库目录文件锁定） |
| `files_panel.py` | 生产组装：`PackageLifecycle(LocalPackageInstaller(<files-data>/plugins), host_session, desired_store, delete_data=配置命名空间, revoke_credentials=secret 撤权, refresh_inventory=控制器 refresh)`；config_store 注入 host_session（F9 预留位）；desired_store 单实例共享 |

## 验证

`tests/plugins/test_package_lifecycle_f11.py`（新，8 项）：
- 安装→激活→更新（经停用、版本切换、双次重扫、受理关闭）；
- 在途任务时更新/卸载拒绝 `PLUGIN_BUSY` 且版本不动；停稳后重试成功；
- 新增权限：无回调拒绝 / 回调拒绝 / 回调放行（见到且仅见到新增集）；
  纯移除与首装不触发确认；
- 卸载分离：代码删除 + 数据按选项（remove_data=True 清 1 条 / False
  保留）+ 凭据总是撤 + desired 清除 + **素材文件逐字节未动**。

回归：tests/plugins + tests/files + native 插件 UI + 面板宿主 ——
**751 通过**。

## 诚实边界

- **中心 UI 的安装/卸载按钮未做**：编排 API 生产就绪（控制器暴露
  `package_lifecycle`）；按钮/文件选择对话框属插件中心 UI 批次。
- 权限重确认的**确认 UI**（权限差异对话框）未做——回调契约已锁定，
  宿主接线前新增权限一律拒绝（保守正确）。
- 更新走"停用后换版本"——不做热替换；重启后新版本生效（与启停语义
  一致，PL-03 边界不变）。

## 隐私

合成包/临时目录；无隐私路径。
