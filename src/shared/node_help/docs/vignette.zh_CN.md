# Vignette 暗角

乘性镜头压角（linear 域 exposure 语义）：`v = 1 - amount × smoothstep(radius-softness, radius, d)`。`圆度` 在圆形（0）与矩形感（1）之间用超椭圆距离插值。纯逐像素，padding 0。

- **强度 / 半径 / 柔和度 / 圆度** 全部可动画。
