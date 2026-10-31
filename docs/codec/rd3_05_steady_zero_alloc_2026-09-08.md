# RD3-05 稳态零分配验收记录

日期：2026-09-08

`tc_decoder` 原本只复用了颜色 DC、Alpha 和 V3 intra scratch；context 批量接口每次调用仍临时申请 `topos_packet_view[count]` 与 `dec_frame_req[count]`。本次将它们纳入 decoder-owned grow-only arena，容量增长时一次替换，正常调用只重置槽内容，不复制外部 packet 字节。

## 验收结果

`test_m10` 在 1 worker 和 8 worker 两档先预热，再连续调用 1000 次 `tc_decoder_decode_batch(count=1)`。通过 `tc_dev_alloc_count()` 观察库内分配：稳态新增分配为 **0**。旧实现按每次两个临时数组计算，等量窗口会产生 2000 次库内分配；该数字是旧代码结构推导值，不是用旧二进制重跑的测量。

复现命令：

```text
cmake --build native/topos_codec/build/debug --target test_m10 --parallel 4
native/topos_codec/build/debug/test_m10
```

输出平面仍由调用方拥有；decoder 销毁时统一释放描述 arena 和已有 scratch。arena 扩容任一分配失败会保留旧容量并返回 `TC_ERR_OUT_OF_MEMORY`，不会丢失旧 context 状态。
