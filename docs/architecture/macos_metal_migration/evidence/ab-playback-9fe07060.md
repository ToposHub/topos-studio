# Topos Edit 播放基线（Metal / OpenGL A/B）

- 采集时间（UTC）: `2026-11-23T20:09:13+00:00`
- 提交: `9fe07060710581e0b9a07198fc845dd1a75a714b`
- 机器: Mac Pro · Intel(R) Core(TM) i9-9900K CPU @ 3.60GHz · AMD Radeon RX 6900 XT, PHL 439P1
- A/B 目录: `bench_out/metal-ab.2026-11-23` · 各配置轮数见「各轮呈现帧」 · 分母为区间唯一期望帧 400
- 显示器电源状态（采集时）: 未记录

呈现帧数取 `frame_presented`（paintGL 之后发射），中位数跨有效轮次；时间线未加载或 0 帧的用例计为无效轮，不进入统计。

| 配置 | 环境变量 | 时间线 | 呈现帧中位数 | 各轮呈现帧 | 无效轮 | 丢帧中位数 | 实际渲染后端 | 实际呈现面 | 解码路径 | 旧帧覆盖 | 槽位未确认复用 | CPU 上传/回读字节 |
|---|---|---|---:|---|---:|---:|---|---|---|---:|---:|---:|
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | dnxhr Original | 327 | 326, 328 | 0 | 73 | `{"opengl": 653}` | `{"gl_viewer": 652}` | `{"ffmpeg_planar_gpu_upload": 653}` | 0 | 0 | 2456616960/7633920 |
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | dnxhr LUT | 314.5 | 306, 323 | 0 | 85.5 | `{"opengl": 630}` | `{"gl_viewer": 627}` | `{"ffmpeg_planar_gpu_upload": 629, "gpu": 3}` | 2 | 0 | 2434940928/8179200 |
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | h264 Original | 387 | 387, 387 | 0 | 13 | `{"opengl": 774}` | `{"gl_viewer": 770}` | `{"macos_vt_zero_copy": 774}` | 0 | 0 | 0/4362240 |
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | h264 LUT | 390 | 390, 390 | 0 | 10 | `{"opengl": 3, "metal": 776}` | `{"gl_viewer": 776}` | `{"gpu": 1, "macos_vt_zero_copy": 778}` | 0 | 0 | 0/7088640 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | dnxhr Original | 330.5 | 327, 334 | 0 | 69.5 | `{"opengl": 660}` | `{"gl_viewer": 658}` | `{"ffmpeg_planar_gpu_upload": 660, "gpu": 1}` | 1 | 0 | 2485518336/6543360 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | dnxhr LUT | 330 | 329, 331 | 0 | 70 | `{"opengl": 660}` | `{"gl_viewer": 657}` | `{"gpu": 1, "ffmpeg_planar_gpu_upload": 659}` | 0 | 0 | 2478292992/8179200 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | h264 Original | 387 | 381, 393 | 0 | 13 | `{"opengl": 776}` | `{"gl_viewer": 772}` | `{"macos_vt_zero_copy": 776}` | 0 | 0 | 0/7088640 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | h264 LUT | 382 | 385, 379 | 0 | 18 | `{"opengl": 762}` | `{"gl_viewer": 760}` | `{"macos_vt_zero_copy": 762}` | 0 | 0 | 0/8179200 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | dnxhr Original | 318.5 | 318, 319 | 0 | 81.5 | `{"opengl": 636}` | `{"gl_viewer": 633}` | `{"ffmpeg_planar_gpu_upload": 636, "gpu": 1}` | 1 | 0 | 2362687488/6543360 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | dnxhr LUT | 324 | 317, 331 | 0 | 76 | `{"opengl": 646}` | `{"gl_viewer": 644}` | `{"ffmpeg_planar_gpu_upload": 646}` | 0 | 0 | 2471067648/7633920 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | h264 Original | 375 | 379, 371 | 0 | 25 | `{"opengl": 4, "metal": 746}` | `{"gl_viewer": 746}` | `{"macos_vt_zero_copy": 750}` | 0 | 0 | 0/4362240 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | h264 LUT | 386 | 392, 380 | 0 | 14 | `{"metal": 769, "opengl": 2}` | `{"gl_viewer": 769}` | `{"macos_vt_zero_copy": 771}` | 0 | 0 | 0/7633920 |
| `native` | `TOPOS_BACKEND_GRAPHICS=metal TOPOS_EDIT_METAL_VIEWPORT=1` | dnxhr Original | 306 | 274, 338 | 0 | 94 | `{"opengl": 611}` | `{"gl_viewer": 610}` | `{"ffmpeg_planar_gpu_upload": 611}` | 0 | 0 | 2507194368/5998080 |
| `native` | `TOPOS_BACKEND_GRAPHICS=metal TOPOS_EDIT_METAL_VIEWPORT=1` | dnxhr LUT | 307.5 | 255, 360 | 0 | 92.5 | `{"opengl": 615}` | `{"gl_viewer": 613}` | `{"gpu": 2, "ffmpeg_planar_gpu_upload": 614}` | 1 | 0 | 2709504000/8724480 |
| `native` | `TOPOS_BACKEND_GRAPHICS=metal TOPOS_EDIT_METAL_VIEWPORT=1` | h264 Original | 370.5 | 389, 352 | 0 | 29.5 | `{"opengl": 4, "metal": 758}` | `{"native_metal": 758}` | `{"macos_vt_zero_copy": 762}` | 0 | 0 | 0/4362240 |
| `native` | `TOPOS_BACKEND_GRAPHICS=metal TOPOS_EDIT_METAL_VIEWPORT=1` | h264 LUT | 397 | 396, 398 | 0 | 3 | `{"metal": 792, "opengl": 2}` | `{"native_metal": 792}` | `{"macos_vt_zero_copy": 794}` | 0 | 0 | 0/7633920 |
