"""B16/B17/B18/B19 · 纯算法核心 oracle 集（各自手册条款的实现）。

- B16（§20/B16）：宿主块回复代际与顺序合同 ``accept_block_reply``——
  旧 generation/乱序/错块长一律拒绝（控制层 oracle；音频回调内不做
  Python 分配/等待）。插件扫描隔离登记 ``QuarantineRegistry``。
- B17（§20/B17）：MTC Quarter Frame 组装 ``MtcAssembler``（错序/重复/
  跳码不发布位置；完整消息才更新）；控制面反馈去环 ``should_apply``
  （origin/token 过滤 + 容量上限，不按 value 去重吞合法重复触摸）；
  ``SyncState`` 机（acquiring→locked→freewheel→lost）。
- B18（§20/B18）：ADM/BWF chna 引用完整性 ``validate_track_uids``
  （UID 形状/唯一性/PCM index 范围；numUIDs > numTracks 合法——
  EBU Tech 3285-s7 §3.2/§4.3，重复 PCM index 不是通用错误）。
- B19（§20/B19）：环回延迟估计 ``estimate_roundtrip_samples``
  （手册 oracle + 带限探针生成），patch 独占检查 ``validate_patch_claims``
  （program/cue/insert 不能抢同一输出悄悄求和）。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# B16 · 块回复代际与顺序
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BlockReply:
    """宿主 worker 的一个块回复（generation/index/frame_count 三元身份）。"""

    generation: int
    index: int
    frame_count: int


def accept_block_reply(reply: BlockReply, generation: int, next_index: int,
                       count: int) -> bool:
    """手册 oracle：只有 (generation, index, frame_count) 与期望完全一致
    的回复被接受——旧 generation（图已换）、重复/回退 index、错块长
    （图边界变化）全部拒绝。"""
    return (reply.generation, reply.index, reply.frame_count) == (
        generation, next_index, count)


@dataclass(frozen=True)
class PluginDescriptor:
    """第三方插件身份（不按显示名称重绑未知插件）。"""

    format: str                 # vst3 | clap | au | aax
    vendor: str
    stable_plugin_id: str
    version: str
    binary_fingerprint: str
    supported_layouts: Tuple[str, ...] = ("stereo",)


class QuarantineRegistry:
    """扫描/加载崩溃隔离（B16 步骤 2）：二进制指纹 → 隔离；重扫解除。

    扫描进程超时/崩溃的插件进 quarantine，以 binary_fingerprint 识别；
    重新扫描成功（同指纹）才解除——不自动给不可用插件开入口。
    """

    def __init__(self, *, max_entries: int = 1024) -> None:
        self._quarantined: "OrderedDict[str, float]" = OrderedDict()
        self._max = int(max_entries)

    def quarantine(self, fingerprint: str, reason: str) -> None:
        key = str(fingerprint)
        self._quarantined.pop(key, None)
        self._quarantined[key] = time.time()
        if len(self._quarantined) > self._max:
            self._quarantined.popitem(last=False)

    def release(self, fingerprint: str) -> bool:
        return self._quarantined.pop(str(fingerprint), None) is not None

    def is_quarantined(self, fingerprint: str) -> bool:
        return str(fingerprint) in self._quarantined

    def entries(self) -> Tuple[Tuple[str, float], ...]:
        return tuple(self._quarantined.items())


# ─────────────────────────────────────────────────────────────────────────────
# B17 · MTC 组装 / 反馈去环 / 同步状态
# ─────────────────────────────────────────────────────────────────────────────

class SyncState(str, Enum):
    """外部同步状态机（B17 数据合同）。"""

    STOPPED = "stopped"
    ACQUIRING = "acquiring"
    LOCKED = "locked"
    FREEWHEEL = "freewheel"
    LOST = "lost"


class MtcAssembler:
    """MTC Quarter Frame 组装（完整合法序列才发布位置）。

    Quarter Frame：0xF1 后 1 字节 = (msg_index << 4) | nibble；序列
    0..7 = 帧 LOW/HIGH、秒 LOW/HIGH、分 LOW/HIGH、时 LOW(+fps 位)/HIGH。
    错序/重复 → 进入 resync（不发布半组装位置）；完整序列 → 位置发布
    并回 acquiring。速度位（高 2 bits of hour-low）按 SMPTE 约定：
    0=24、1=25、2=29.97DF、3=30（本组装器只解析位置，fps 上报调用方）。
    """

    _EXPECTED_ORDER = tuple(range(8))

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._nibbles: List[int] = [0] * 8
        self._have: Set[int] = set()
        self._expect_index = 0

    def feed_quarter_frame(self, data_byte: int) -> Optional[Dict[str, int]]:
        """喂一个 Quarter Frame 数据字节；完整合法序列返回位置 dict。"""
        index = (data_byte >> 4) & 0x07
        nibble = data_byte & 0x0F
        if index != self._expect_index:
            # 错序/重复/跳码 → resync，不发布
            self.reset()
            return None
        self._nibbles[index] = nibble
        self._have.add(index)
        self._expect_index += 1
        if self._expect_index > 7:
            frames = self._nibbles[0] | (self._nibbles[1] << 4)
            seconds = self._nibbles[2] | (self._nibbles[3] << 4)
            minutes = self._nibbles[4] | (self._nibbles[5] << 4)
            hours_low = self._nibbles[6]
            hours = (hours_low & 0x1F) | (self._nibbles[7] << 4)
            fps_code = (hours_low >> 5) & 0x03
            fps = {0: 24, 1: 25, 2: 30, 3: 30}[fps_code]  # 2=29.97DF 按 30 计
            position_frames = ((hours * 3600 + minutes * 60 + seconds)
                               * fps + frames)
            self.reset()
            return {"hours": hours, "minutes": minutes, "seconds": seconds,
                    "frames": frames, "fps_code": fps_code,
                    "position_frames": position_frames}
        return None


def should_apply(origin: str, event_id: int, local_origin: str,
                 seen: "OrderedDict[Tuple[str, int], float]", *,
                 capacity: int = 256, window_s: float = 30.0,
                 now: Optional[float] = None) -> bool:
    """控制面反馈去环（手册 oracle + 生产约束）。

    规则：本地 origin 的回声不应用；已见 (origin, event_id) 在时间窗内
    不重复应用。``seen`` 有容量/时间窗上限——只按 value 去重会吞掉下一
    次合法的重复触摸（手册明示）。
    """
    if origin == local_origin:
        return False
    now = time.time() if now is None else now
    key = (str(origin), int(event_id))
    # 过期清理（时间窗）
    expired = [k for k, ts in seen.items() if now - ts > window_s]
    for k in expired:
        seen.pop(k, None)
    if key in seen:
        return False
    seen[key] = now
    while len(seen) > capacity:
        seen.popitem(last=False)
    return True


class SyncStateMachine:
    """外部同步状态机：观测超时 → freewheel → lost（录音策略由 session 定）。"""

    def __init__(self, *, lock_after_s: float = 2.0,
                 freewheel_after_s: float = 0.5,
                 lost_after_s: float = 5.0) -> None:
        self.state = SyncState.STOPPED
        self._lock_after = float(lock_after_s)
        self._freewheel_after = float(freewheel_after_s)
        self._lost_after = float(lost_after_s)
        self._last_observation: Optional[float] = None
        self._first_observation: Optional[float] = None

    def observe(self, now: float) -> SyncState:
        """每次收到合法 timecode 观测调用；返回当前状态。

        LOST 后的重新观测重新锚定 acquiring 窗口（失锁后不沿用旧计时）。
        """
        if self._first_observation is None or self.state == SyncState.LOST:
            self._first_observation = now
        self._last_observation = now
        elapsed = now - self._first_observation
        if elapsed >= self._lock_after:
            self.state = SyncState.LOCKED
        else:
            self.state = SyncState.ACQUIRING
        return self.state

    def poll(self, now: float) -> SyncState:
        """观测间隔轮询：超时降级（freewheel→lost），恢复观测回到 acquiring。"""
        if self._last_observation is None:
            self.state = SyncState.STOPPED
            return self.state
        since = now - self._last_observation
        if since <= self._freewheel_after:
            if self.state in (SyncState.FREEWHEEL, SyncState.LOST,
                              SyncState.STOPPED):
                self._first_observation = None
                return self.observe(now)
            return self.state
        if since <= self._lost_after:
            self.state = SyncState.FREEWHEEL
        else:
            self.state = SyncState.LOST
        return self.state


# ─────────────────────────────────────────────────────────────────────────────
# B18 · ADM/BWF chna 引用完整性
# ─────────────────────────────────────────────────────────────────────────────

def validate_track_uids(uids: List[Tuple[str, int]],
                        channel_count: int) -> None:
    """手册 oracle（EBU Tech 3285-s7 §3.2/§4.3 语义）。

    - UID 形状 ATU_XXXXXXXX（hex）；全局唯一；PCM index ∈ [0, channels)。
    - numUIDs > numTracks 合法（不同时间的 UID 可复用同一 PCM 轨）——
      **重复 PCM index 不是通用错误**；全通道覆盖如为首版限制，写在
      具体 profile，不当成 ADM 通用规则。
    """
    if type(channel_count) is not int or channel_count < 1:
        raise ValueError("invalid PCM channel count")
    import re

    names = [uid for uid, _ in uids]
    if len(names) != len(set(names)):
        raise ValueError("duplicate UID")
    for uid, index in uids:
        if re.fullmatch(r"ATU_[0-9A-Fa-f]{8}", uid) is None:
            raise ValueError("invalid UID shape")
        if type(index) is not int or not 0 <= index < channel_count:
            raise ValueError("PCM index out of range")


def pack_chna_chunk(num_tracks: int, uids: List[Tuple[str, int]]) -> bytes:
    """打包最小 chna chunk 载荷（EBU Tech 3285-s7 布局：NumTracks u32、
    NumUIDs u32、每条 16B = u16 trackIndex（磁盘从 1 起）+ u16 ref + 12B
    UID ASCII）。本函数是最小编码器，完整 chunk 还需 chunk 头与 profile
    校验（B18 手册：XML/profile validator 另立）。"""
    import struct

    payload = struct.pack("<II", int(num_tracks), len(uids))
    for uid, index in uids:
        ascii_uid = uid.encode("ascii")
        if len(ascii_uid) != 12:
            raise ValueError("UID must be 12 ASCII chars")
        payload += struct.pack("<HH", int(index) + 1, 0) + ascii_uid
    return payload


def parse_chna_chunk(payload: bytes) -> Tuple[int, List[Tuple[str, int]]]:
    """解析 chna 载荷 → (num_tracks, [(uid, pcm_index0), ...])。"""
    import struct

    if len(payload) < 8:
        raise ValueError("chna payload too short")
    num_tracks, num_uids = struct.unpack_from("<II", payload, 0)
    offset = 8
    uids: List[Tuple[str, int]] = []
    for _ in range(num_uids):
        if offset + 16 > len(payload):
            raise ValueError("chna payload truncated")
        index, _ref = struct.unpack_from("<HH", payload, offset)
        uid = payload[offset + 4: offset + 16].decode("ascii", errors="strict")
        uids.append((uid, int(index) - 1))
        offset += 16
    return int(num_tracks), uids


# ─────────────────────────────────────────────────────────────────────────────
# B19 · 环回延迟估计与 patch 独占
# ─────────────────────────────────────────────────────────────────────────────

def bandlimited_probe(sample_rate: int, seconds: float = 0.05, *,
                      seed: int = 1900, lo_hz: float = 200.0,
                      hi_hz: float = 8000.0,
                      amplitude: float = 0.05) -> np.ndarray:
    """带限低电平 MLS 风格探针（B19：不给未知外设满幅脉冲）。"""
    rng = np.random.default_rng(seed)
    n = int(sample_rate * seconds)
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    mask = ((freqs >= lo_hz) & (freqs <= hi_hz)).astype(np.float64)
    shaped = np.fft.irfft(spec * mask, n=n)
    peak = float(np.max(np.abs(shaped))) or 1.0
    return (shaped / peak * amplitude).astype(np.float32)


def estimate_roundtrip_samples(sent: np.ndarray,
                               returned: np.ndarray) -> int:
    """环回延迟估计（手册 oracle）：|corr| 峰位置 = 整数延迟样本。"""
    sent = np.asarray(sent, dtype=np.float64).ravel()
    returned = np.asarray(returned, dtype=np.float64).ravel()
    if sent.size == 0 or returned.size < sent.size:
        raise ValueError("invalid calibration window")
    correlation = np.correlate(returned, sent, mode="valid")
    return int(np.argmax(np.abs(correlation)))


def validate_patch_claims(claims: Dict[str, Tuple[int, ...]]) -> List[str]:
    """物理 patch 独占检查：同一输出通道被 program/cue/insert 同时占用
    → 冲突（B19 步骤 1：不允许悄悄求和；共享必须显式配置）。

    claims: {claim_id: (output_channel, ...)}；返回冲突描述列表。
    """
    owners: Dict[int, List[str]] = {}
    for claim_id, channels in claims.items():
        for ch in channels:
            owners.setdefault(int(ch), []).append(str(claim_id))
    return [
        f"output channel {ch} claimed by {', '.join(ids_)}"
        for ch, ids_ in sorted(owners.items()) if len(ids_) > 1
    ]


__all__ = [
    "BlockReply",
    "MtcAssembler",
    "PluginDescriptor",
    "QuarantineRegistry",
    "SyncState",
    "SyncStateMachine",
    "accept_block_reply",
    "bandlimited_probe",
    "estimate_roundtrip_samples",
    "pack_chna_chunk",
    "parse_chna_chunk",
    "should_apply",
    "validate_patch_claims",
    "validate_track_uids",
]
