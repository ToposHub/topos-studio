# CW-B00 冻结参考行为:素材基线与行为矩阵

日期:2026-11-12。对应计划 `docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B00。

本文档分两部分:

1. **素材基线**(机器可复核):三份核心 fixture 的 probe/manifest/锚点帧,由
   `scripts/probe_warper_fixtures_b00.py` 生成,数据在 `fixtures_manifest.json`。
2. **行为矩阵**(人工证据分级):把已知语义按「实测相符 / Topos 声明 / 未确认」三级冻结。
   **只有「实测」与「Topos 声明」可以进入 B01 正式 schema;「未确认」一律不写成规范。**

Resolve Studio 20 实机观察部分引用计划 §1.2 已记录的截图与观察(前序会话在本机完成);
本轮 B00 未新增实机操作记录,需实机补充的项目单列在 §4,不得在补齐前宣称为 Resolve 事实。

---

## 1. 素材 probe 基线(全部来自实际解码,非文件名推断)

| fixture | codec | 分辨率 | pix_fmt | 帧率 | 时长/帧数 | 颜色标签(probe) | sha256(前16位) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `2K_h264.mp4` | h264 | 1920×1080 | yuv420p 8bit | 25/1 | 62.89s / 1572帧 | BT.709 prim/trc/matrix + limited range | `3d420027cb4b6afc` |
| `2K_prores422.mov` | prores | 1920×1080 | yuv422p10le | 25/1 | 19.48s / 487帧 | BT.709 prim/trc/matrix + limited range | `390e62a2dc781b0c` |
| `2K_DNxHR HQX 10bit.mov` | dnxhd | 1920×1080 | yuv422p10le | 25/1 | 19.48s / 487帧 | BT.709 prim/trc/matrix + limited range | `91d31c0b157d5575` |

- 三份素材 PTS 全部单调递增;首帧 PTS=0。
- 锚点帧:每份素材的首/中/尾帧记录 `frame_index`、`pts`、`pts_seconds`、`time_base`、
  整帧 `rgb24` 像素 sha256;`frames/` 下另有 960px PNG 仅供人工目检,不是色彩基准。
- 连续播放区间:中段帧后 50 帧顺序解码,逐帧 PTS+像素 hash(487 帧素材即 244~293 帧区间)。
- 解码器:PyAV 18.1.0(内嵌 FFmpeg),macOS 24.3 x64;像素 hash 跨解码器版本不保证逐位一致,
  跨版本差异先比对 manifest 再归因(见 manifest `decoder.note`)。
- 音频仅 h264 fixture 携带(aac 48k stereo),与 Warper 无关。
- 其余 36 份素材(含 4K 性能候选)已轻量登记于 manifest `sibling_assets`,供 B08 选材;
  未经本 manifest 冻结的素材不得冒充 fixture 通过验收。

## 2. 三模式行为矩阵

状态图例:**[实测]**=本机 Resolve 20 截图/操作记录(计划 §1.2);**[Topos]**=Topos 声明的
可验证目标行为(允许与 Resolve 近似,但必须自洽、可测);**[未确认]**=待实机证据,禁止进 schema。

### 2.1 Hue–Saturation

| 项 | 观察记录 | 状态 | B01+ 采用的规范 |
| --- | --- | --- | --- |
| Hue/Saturation Resolution 独立 | 各自下拉,候选 6/8/12/16/24 | [实测]候选;[未确认]是否完整 | 两轴独立尺寸;候选集按 UI 现值 6/8/12/16/24,标记 `candidates_verified=false` |
| Link | 存在开关 | [实测] | 开启时两轴同步变更,单次可撤销命令 |
| 色彩空间 HSV/HSL/HSP | 底栏可选 | [实测] | HSV=legacy(见 §2.4);HSL/HSP 坐标变换由 B03 冻结常数 |
| 网格语义 | 六角/双网格显示 | [Topos] | U=hue∈[0,1) 环绕,V=sat∈[0,1] clamp;显示几何与算法坐标分离 |
| Hue/Sat/Luma 数值表达 | 数值框+滑条,多选可混合 | [未确认]绝对/偏移/聚合 | Topos:偏移量(offset),单位=位移;混合选择显示 mixed 态不写入 |
| Auto Lock 1/2 Points | 两档 | [实测]存在;[未确认]拓扑 | Topos:1=直接邻接锁定,2=两级邻接;约束半径断言进 B04 测试 |
| Smooth(Hue/Sat) | 分别作用于分量 | [实测]UI 行为 | 选点集相邻位移低通,分量独立;一次手势一次 undo |
| Luma | 独立展示值 | [实测]UI | 需第三分量数据通道,不得塞入旧两分量纹理 |

### 2.2 Chroma–Luma

| 项 | 观察记录 | 状态 | B01+ 采用的规范 |
| --- | --- | --- | --- |
| Grid 1 / Grid 2 并列 | 两方形场 | [实测] | 两套独立具名网格,各自尺寸/约束/ID |
| Chroma/Luma Resolution | 独立下拉 | [实测]存在 | 独立尺寸;CL 矩形候选含 4(当前 UI),`candidates_verified=false` |
| Axis Angle | DragValue | [实测]存在;[未确认]单位/方向 | Topos:度,范围[-180,180],默认0;方向语义 B05 定義并测试 |
| Auto Pin Columns/Rows/Single | 三方式 | [实测]存在 | 自动约束生成;与手动 Pin 的取消/重置政策 B05 定义 |
| 两网格合成顺序 | 未观察 | [未确认] | Topos:顺序合成 Grid1→Grid2(标记近似,可验证可撤销) |
| 灰阶亮度单调性 | — | [Topos] | 固定 chroma 时 luma 单调;B05 测试断言 |

### 2.3 Chroma Warp

| 项 | 观察记录 | 状态 | B01+ 采用的规范 |
| --- | --- | --- | --- |
| Normal / Point-to-Point 笔画 | 工具按钮 | [实测] | 两种模式独立求解过程(B06),约束=源/目标颜色对 |
| Add Pin / Select / Delete | 工具存在 | [实测] | Pin=固定约束;删除可撤销 |
| Pin Chroma Range 默认 0.040 | 实机数值 | [实测]默认值 | 域 Topos:[0,1],默认 0.040 |
| Tonal Range Low/High 默认 1/1 | 实机数值 | [实测]默认值;[未确认]单位 | Topos:权重域[0,1],Low≤High 校验 |
| Pivot 默认 0.5 | 实机数值 | [实测]默认值 | 域[0,1] |
| Exposure 默认 0 | 实机数值 | [实测]默认值 | Topos 域[-8,8](UI 现值,未确认 Resolve 界限) |
| 笔画求解算法 | 私有 | — | Topos 自定义可验证求解(B06),不宣称与 Resolve 逐位一致 |

### 2.4 公共 Tools / Range

| 项 | 观察记录 | 状态 | B01+ 采用的规范 |
| --- | --- | --- | --- |
| Select/Deselect/Draw Selection/反选 | 纯 UI | [实测] | 选择不进历史、不改模型 |
| Pin/De-pin、Column、Ring/Row、All | 选择类工具 | [实测]存在 | 选点转 Pin=实质约束变更,单命令进历史 |
| Pull/Push、falloff 增减 | 观察存在 | [未确认]精确语义 | Topos:Pull/Push=沿径向收缩/扩张位移,falloff=影响半径权重(B04 测试冻结) |
| Reset Selection / Reset Pins | 两 reset 分开 | [实测] | 分别作用于选点位移 / 全部 Pin;`on_reset()` 仅当前模式,`reset_all()` 全局(既有 UI 语义) |
| HS Auto Lock 1/2、范围色带、Hue/Sat/Luma 数值、Smooth/Reset | 见 §2.1 | — | — |
| legacy HSV 语义 | 现 GLSL: `rgb2hsv(abs(color))` 采样 Δhue/Δsat,低饱和 fade,保留符号 | [实测]代码事实 | 旧工程标记 `legacy_hsv_v1`,迁移不得改变画面(B03 golden fixture) |

## 3. 已知差异清单(Topos vs Resolve,允许存在,单列不算缺陷)

1. 全局面板背景/按钮/DragValue/ChevronComboBox 宽度与 Resolve 44px 字段不同(共享 Topos 样式);
2. HS 默认展示空间为 HSP(待接入 UI 惯例),宿主回填 legacy 数据时切回 HSV;
3. CL 场配色为归一化测量的近似调色板,非 Resolve 场的 ICC 精确复刻;
4. CW 场背景为显示专用色度图,非真实密度云(真实云待 B07);
5. 数值域/步进按 Topos 全局控件惯例取整,不与 Resolve 逐位对齐。

## 4. 待人工实机复核清单(补齐前均为「未确认」,禁止写入 schema 为事实)

- [ ] HS/CL 完整分辨率候选列表与禁用态(CL 是否含 4;HS 是否仅 6/8/12/16/24)
- [ ] Axis Angle 单位、方向、默认值与重置行为
- [ ] HS Hue/Sat/Luma 数值是绝对值/偏移/聚合;多选混合显示规则
- [ ] Auto Lock 1/2 Points 精确邻域拓扑;Pin 拖动政策;Pull/Push 与 falloff 精确语义
- [ ] CL 两网格是顺序合成还是互补投影;Auto Pin 三方式优先级
- [ ] CW Tonal Range 单位;Exposure 精确范围;笔画相交/矛盾约束行为
- [ ] 各 gesture 的撤销次数(预期每 gesture 1 次)
- [ ] 密度云采样源(输入/输出)与刷新行为

## 5. B03+ 生成夹具计划(声明,供后续阶段实现)

hue sweep(含环绕两侧)、saturation ramp、luma ramp、灰阶、色卡(六主/次色)、
肤色样张、负通道、RGB>1、极大/极小有限值——全部程序生成 float 线性小图,
与三份真实素材 fixture 分开归档;B03 的 CPU/GLSL/MSL 三端比对以这些生成夹具为准,
真实素材 fixture 用于 B02/B07/B08/B09 的链路与性能验证。

## 6. 复核方式

```sh
.venv/bin/python scripts/probe_warper_fixtures_b00.py            # 全量(含 sha256)
.venv/bin/python scripts/probe_warper_fixtures_b00.py --skip-hash  # 快速重跑
```

输出:`docs/validation/color_warper_b00/fixtures_manifest.json` + `frames/*.png`。
