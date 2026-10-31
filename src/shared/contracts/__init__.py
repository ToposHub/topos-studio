"""跨端契约（DESK 系列）。

本包只放**双端共享的契约数据与生成器**（桌面 Python / 移动 Dart），
不依赖任何 feature 模块——移动侧照抄语义即可对拍。

- ``timebase``：时间精度向量（DESK-05A）
- ``effect_catalog``：效果/转场目录（DESK-06B）
- ``edit_semantics``：磁性主轨 / A/V 链接 / ripple 语义（DESK-05B）
- 包结构契约在 ``src/shared/project/package_spec.py``（DESK-01A）
"""
