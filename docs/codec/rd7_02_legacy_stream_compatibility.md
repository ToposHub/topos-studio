# RD7-02：旧流兼容与 legacy reduced fallback

## 已完成

- V1–V6 仍由现有 reader 读取；本次没有修改旧 packet，也没有在打开或播放时回写源文件。
- `tc_movie_info()` 复用 `topos_movie_info.reserved[0]` 返回 tpcC 的轨级 `bitstream_major`。这是已有保留槽，不改变结构大小或 ABI 版本。
- Python `ToposMovieSummary.bitstream_major` 和 `ToposMediaSource.diagnostics` 现在能报告版本与兼容状态：
  - `stream_compatibility=legacy_v1_v6`
  - `legacy_reduced_fallback=true`：旧流采用固定比例 native reduced request
  - `legacy_reduced_fallback=false`：当前请求不是固定比例 reduced，不能把它误报成低频 fallback
- 使用比 RD7-01 更旧的 native dylib 时，版本槽为 `0`，诊断为
  `legacy_metadata_unavailable`；不会猜测版本或宣传 V7 能力。

## 保护边界

`V7-A band decode` 和 `V7-B scalable base` 仍未置 capability bit。旧流不会自动升级、转码或重写；未知布局仍由 native parser 按错误码拒绝。

## 验证

- `test_abi`、`test_abi_compat`、`test_mov`：通过
- `tests/native/test_topos_binding.py` + `tests/media/test_topos_source.py`：53 passed
- 回归覆盖：旧流版本透传、fixed-ratio reduced 实际输出、诊断字段和源文件字节不变
