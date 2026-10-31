# 视频 V8/V9：生态与发布能力矩阵

状态：当前帧内 profile 的内部 SDK、Topos Player/Source、素材管理器和图片抽帧
路径已可用；外部宿主和跨平台安装发布仍是明确 deferred。

| 能力 | 当前状态 | 证据/边界 |
| --- | --- | --- |
| C ABI / Python 参考绑定 | supported | `docs/image/SDK.md`、ABI smoke、`src/shared/codec/topos_binding.py` |
| Topos MOV 读写、播放、seek | supported | `ToposMovieFile`、`ToposMediaSource`、V6/V7 回归 |
| `.toos` 抽帧与素材管理预览 | supported | `ToposFrameExtractor`、file explorer tests |
| OIIO | source-delivered / experimental | `docs/image/ADR-I020-external-ecosystem.md`；无 SDK 时仅 stub syntax gate |
| FFmpeg 原生 demux/codec | unsupported | MOV 容器存在不等于 FFmpeg 已注册 Topos codec |
| Resolve / Premiere / Nuke | unsupported | 需要各自 SDK、ABI、授权和真实宿主往返证据 |
| macOS SDK smoke | verified locally | `build_sdk.sh --smoke`；当前机器结果不替代其它平台 |
| Linux / Windows ABI 与安装包 | deferred | 需真实 CI runner 和 DLL/SO load test 证据 |
| 签名、公证、Windows 签名 | deferred/ad-hoc only | 凭据和发行 runner 未在本机具备 |

发布规则：未经过真实宿主往返的适配器不得进入 supported capability；V3/V5 的
inter profile 必须通过 capability 协商，不能让 V8 宿主误以为它具备当前 all-intra
的随机 seek、零重编码抽帧和低延迟语义。
