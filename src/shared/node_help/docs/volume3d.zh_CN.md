# Volume 3D 体积

稠密体积渲染（CPU raymarch MVP，Beer–Lambert 前向积分）。

**依赖门（约束 13）**：pyopenvdb 在当前环境不可用（spike 结论）——`.vdb` 文件会给出明确的依赖错误而不是崩溃；请先用 `tools/convert_vdb.py`（在装有 OpenVDB 的环境运行）离线转换为 `.npz` 稠密体积格式 `{"density": (D,H,W) float32}`。GPU scene3d raymarch pass 待依赖解决后立项。

- **密度倍增** 可动画；**步进数** preview 档自动减半；**吸收颜色** linear 域
