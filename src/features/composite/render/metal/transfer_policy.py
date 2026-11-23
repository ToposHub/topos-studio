"""阶段 5.6 · Metal 合成帧的设备/代际传输策略（纯逻辑，无 GPU）。

``CompositeGpuFrameHandle(backend="metal")`` 的纹理属于生产它的那台
MTLDevice。本模块定义**唯一一条**传输策略（迁移计划 5.6「设备或代际
不匹配按已定义传输策略处理」，此前没有定义，此处补齐并写进架构文档）：

==== =========================  ===========================================
判定                            动作
==== =========================  ===========================================
1. 句柄不可用                  :attr:`REFUSE`——按 ``fallback_reason``
                                回退 CPU/GL，不搬运。
2. 消费端不是 Metal（如 GL     :attr:`READBACK_TRANSFER`——Metal 纹理
   视口/编码器）                GL 消费者读不了：唯一跨后端通路是
                                ``renderer.readback`` 读回 CPU，再由消费
                                端自行上传；租约在读回完成后归还。
3. 设备身份不一致（device_     :attr:`READBACK_TRANSFER`——同上，绝不把
   identity 不匹配）            外设备的纹理名交给消费者。
4. 代际过期（generation 与      :attr:`DROP_STALE`——旧帧只用于回收：
   消费端不匹配）               立即 ``release()`` 归还租约，不上屏、
                                不搬运（与 handle 契约的
                                ``is_presentable`` 一致）。
5. 其余（同设备 + 同代际）      :attr:`PRESENT`——直接消费 GPU 纹理，
                                零拷贝；读完后 ``release()``。
==== =========================  ===========================================

规则 4 优先于 2/3：过期帧无论设备是否匹配都不搬运——搬运半旧帧只会
掩盖跳变。策略函数是纯判定；执行（读回/释放）由调用方在拥有者线程做。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from src.shared.gpu.composite_gpu_frame_handle import CompositeGpuFrameHandle


class TransferAction(Enum):
    """一条 Metal 帧的消费端处置动作。"""

    PRESENT = "present"                    # 同设备同代际：直接消费
    READBACK_TRANSFER = "readback_transfer"  # 跨设备/跨后端：读回 CPU 转运
    DROP_STALE = "drop_stale"              # 代际过期：回收租约，不搬运
    REFUSE = "refuse_unavailable"          # 句柄不可用：走回退路径


def decide_metal_transfer(
    handle: CompositeGpuFrameHandle,
    *,
    consumer_backend: str = "metal",
    consumer_device_identity: str = "",
    consumer_generation: Optional[int] = None,
) -> TransferAction:
    """按上面的策略表判定一帧 Metal 句柄的消费端处置动作。"""
    if not getattr(handle, "available", False):
        return TransferAction.REFUSE
    if consumer_generation is not None \
            and handle.generation != consumer_generation:
        return TransferAction.DROP_STALE
    if str(handle.backend) != str(consumer_backend):
        return TransferAction.READBACK_TRANSFER
    if (consumer_device_identity
            and handle.device_identity
            and handle.device_identity != consumer_device_identity):
        return TransferAction.READBACK_TRANSFER
    return TransferAction.PRESENT
