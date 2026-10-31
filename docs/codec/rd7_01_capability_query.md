# RD7-01：统一 capability query

已加入 `tc_query_capabilities()` 及 Python `ToposCodec.capabilities()`。查询结果
明确区分“当前公共 API 已可用”和“内部原语存在但尚未形成可发布 V7 码流”：

- 当前公共支持：完整解码、固定比例 reduced、AUTO_2K、CPU surface、host-visible GPU surface；
- 当前 ABI 最大 visible 尺寸：16384×16384；
- AUTO_2K 长边上限：2048；
- V7-A band frame decode：已置位。公共 `tc_frame_decode()`、固定比例
  `tc_frame_decode_reduced()` 和统一 `tc_frame_decode_request()` 会识别 V7-A
  packet，并按请求读取对应 band；V7-A 仍不是默认 MOV writer。
- V7-B scalable base：暂不置位，直到公共 reader/writer 真正消费 ≤2K base 与
  enhancement residual 布局并通过 RD4-08 体积/编码速度门。当前已有显式
  `tc_frame_encode_scalable()` opt-in writer 和公共 full/reduced reader；不置位
  只表示默认 rollout 尚未获批，不表示 V7-B elementary/MOV 路径不可用。

native 头文件、Python mirror、`capability_manifest.json` 和测试共用同一组
feature bits，避免播放器在 V7-B 尚未完成时误走默认路径。
