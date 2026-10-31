# Lens Flare 镜头光晕

程序化光晕 MVP：**ghost 序列**（中心↔光源轴上的镜像亮点，间距/衰减/大小可调）+ **halo 环**（光源周围可调半径）+ **变形拉丝**（横向 anamorphic 条纹）。screen / add 两种混合；`遮挡感知` 用光源处输入 alpha 衰减光晕。全帧 screen-space，padding 0；draft 档 ghost 数减半。

- **光源 X / Y**：屏幕 UV（可超出画幅），可动画。
- 遮挡感知增强、光源被遮挡自动衰减属于可选开关（默认关）。
