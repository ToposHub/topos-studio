# RD2-09：V7-A 真实编码器桥接

状态：已实现（内部参考路径，尚未进入默认 writer）。

`tc_v7_frame_encode()` 复用现有颜色编码器的平面准备、前向 DCT、量化和
token 生成任务，在同一次量化结果上完成：

1. 保留原始 `DC + (run, level)` token；
2. 单遍转换为 B0–B4 的 band-local token 数组；
3. 为每个 slice 编码五个独立 band payload；
4. 由 TPLD directory writer 直接组包并计算 segment/directory CRC。

因此这条路径没有第二次 DCT 或量化，也没有把完整源平面复制成代理文件。
V7-A reader 可以按 `max_band` 只读取 B0 到目标频带，full 路径则读取五段。

## 当前边界

- 入口位于内部 `native/topos_codec/src/codec/codec.h`，不属于公共 ABI；
- 仅支持无 Alpha 的颜色帧，Alpha 会明确返回 `TC_ERR_NOT_IMPLEMENTED`；
- 仍输出源空间 block 几何，不能把 V7-A 解释成 2K spatial base；
- 默认 V1–V6 writer、公共 capability 位和 MOV/TPIM rollout 不变；
- V7-B base + enhancement residual 仍需独立实现并通过体积/编码速度门禁。

## 验收

`unit_v7_frame_encoder` 使用真实 4:2:2 三平面输入，验证：

- V7-A packet header、TPLD slice/segment 数量和 CRC 可读；
- full V7-A reader 读取全部 band；
- V7-A full 重建与同配置 V2-Rice full decode 逐平面逐字节一致；
- 既有 V7-A frame reader、band decoder 和 token partition tests 保持通过。
