"""G2：image_factory 接管——批处理 command（真实选中数组 + 输出计划）。

G 节行验收：**色彩/HDR、逐项结果、取消/关闭，生成结果同一索引入口回库**。

* :class:`ImageFactoryService`——图片批处理域层：真实选中数组（全部
  项，不是单文件）；输入快照复用 G1 的 ``InputSnapshot``；输出计划 +
  冲突政策（rename/skip/overwrite，执行期 rename 解析同 G1）；执行用
  Pillow **同进程**（builtin 可信，F10 门禁放行）——**色彩/HDR 保真**：
  ICC 配置文件逐项透传（info["icc_profile"] → save(icc_profile=…)），
  16-bit PNG（mode I;16 / I;16B / I）与 float TIFF 原位深读写；逐项
  协作取消（``threading.Event``，项间检查）；每项 deadline 兜底。
* **同一索引入口回库**：``BatchResult`` 携带 source→output 映射，完成
  回调交给宿主；宿主用**同一**刷新入口（``refresh`` + 素材库重扫
  ``start_scan``）让生成结果出现在同一索引里——不开第二套索引。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from src.features.files.format_factory_service import InputSnapshot

ITEM_DEADLINE_SEC = 60.0

PRESETS: dict[str, dict[str, object]] = {
    "png": {"title": "PNG（无损，保留位深）", "suffix": ".png",
            "format": "PNG"},
    "jpg-q90": {"title": "JPEG（质量 90）", "suffix": ".jpg", "format": "JPEG",
                "kwargs": {"quality": 90}},
    "webp-q85": {"title": "WebP（质量 85）", "suffix": ".webp",
                 "format": "WEBP", "kwargs": {"quality": 85}},
    "png-half": {"title": "PNG 长边减半（保留位深）", "suffix": ".png",
                 "format": "PNG", "resize": 0.5},
}


@dataclass(frozen=True)
class PlanItem:
    source: InputSnapshot
    output_path: str
    conflict: str


@dataclass(frozen=True)
class BatchResult:
    """一批的结果：逐项 ok/fail + source→output（回库映射）。"""

    outputs: tuple[tuple[str, str], ...] = ()    # (source, output)
    failures: tuple[tuple[str, str], ...] = ()   # (source, reason)


class ImageFactoryService:
    """图片批处理：计划 → 执行（Pillow 同进程、色彩/HDR 保真、可取消）。"""

    def __init__(self, *, item_deadline_sec: float = ITEM_DEADLINE_SEC) -> None:
        self._item_deadline = float(item_deadline_sec)
        self._cancel = threading.Event()

    # ------------------------------------------------------------ 计划
    def build_plan(self, inputs: Iterable[str], preset: str, *,
                   conflict_policy: str = "rename") -> tuple[PlanItem, ...]:
        if preset not in PRESETS:
            raise ValueError(f"未知预设：{preset}")
        if conflict_policy not in ("rename", "skip", "overwrite"):
            raise ValueError(f"未知冲突政策：{conflict_policy}")
        spec = PRESETS[preset]
        plan: list[PlanItem] = []
        for raw in inputs:
            snapshot = InputSnapshot.capture(str(raw))
            stem = Path(snapshot.path).stem
            suffix = str(spec["suffix"])
            candidate = str(Path(snapshot.path).with_name(stem + suffix))
            conflict = ""
            if os.path.exists(candidate):
                conflict = "exists"
                if conflict_policy == "rename":
                    index = 1
                    while os.path.exists(
                            f"{candidate[:-len(suffix)]}-{index}{suffix}"):
                        index += 1
                    candidate = f"{candidate[:-len(suffix)]}-{index}{suffix}"
                    conflict = "exists(resolved)"
            plan.append(PlanItem(source=snapshot, output_path=candidate,
                                 conflict=conflict))
        return tuple(plan)

    # ------------------------------------------------------------ 执行
    def cancel(self) -> None:
        self._cancel.set()

    def _reset(self) -> None:
        self._cancel.clear()

    def execute(self, plan: tuple[PlanItem, ...], preset: str, *,
                conflict_policy: str = "rename",
                on_item: Callable[[int, int, str, bool, str], None] | None = None,
                on_done: Callable[[BatchResult], None] | None = None,
                ) -> None:
        """同步逐项执行（宿主放 worker 线程；项间检查取消）。

        ``on_item(index, total, path, ok, message)`` 逐项进度；
        ``on_done(BatchResult)`` 收尾（含取消中断的如实部分结果）。
        """
        self._reset()
        from PIL import Image

        spec = PRESETS[preset]
        outputs: list[tuple[str, str]] = []
        failures: list[tuple[str, str]] = []
        total = len(plan)
        started = time.monotonic()
        for index, item in enumerate(plan):
            if self._cancel.is_set():
                failures.append((item.source.path, "已取消"))
                if on_item is not None:
                    on_item(index, total, item.source.path, False, "已取消")
                continue
            if time.monotonic() - started > self._item_deadline * max(1, total):
                failures.append((item.source.path, "整批超过预算"))
                if on_item is not None:
                    on_item(index, total, item.source.path, False, "整批超过预算")
                continue
            ok, message = self._execute_one(item, spec, conflict_policy,
                                            Image=Image)
            if ok:
                outputs.append((item.source.path, message))
            else:
                failures.append((item.source.path, message))
            if on_item is not None:
                on_item(index, total, item.source.path, ok, message)
        if on_done is not None:
            on_done(BatchResult(outputs=tuple(outputs),
                                failures=tuple(failures)))

    def _execute_one(self, item: PlanItem, spec: dict[str, object],
                     conflict_policy: str, *, Image) -> tuple[bool, str]:
        if not item.source.matches_disk():
            return False, "输入已变化（快照失配）"
        destination = item.output_path
        if conflict_policy == "skip" and os.path.exists(destination):
            return False, f"输出已存在（skip 政策跳过）：{destination}"
        if conflict_policy == "rename" and os.path.exists(destination):
            suffix = Path(destination).suffix
            stem = str(destination)[:-len(suffix)] if suffix else str(destination)
            index = 1
            while os.path.exists(f"{stem}-{index}{suffix}"):
                index += 1
            destination = f"{stem}-{index}{suffix}"
        try:
            with Image.open(item.source.path) as image:
                kwargs = dict(spec.get("kwargs", {}) or {})
                resize = spec.get("resize")
                working = image
                if resize:
                    width = max(1, int(image.width * float(resize)))
                    height = max(1, int(image.height * float(resize)))
                    working = image.resize((width, height),
                                           getattr(Image, "LANCZOS"))
                # 色彩保真：ICC 配置文件透传；HDR/高位深由 Pillow 按模式
                # 原位深写（16-bit PNG/float TIFF 不压 8-bit）
                icc = image.info.get("icc_profile")
                if icc is not None:
                    kwargs["icc_profile"] = icc
                tmp = destination + ".part"
                working.save(tmp, format=str(spec["format"]), **kwargs)
            os.replace(tmp, destination)      # 原子交付（无 .part 残留）
            return True, destination
        except (OSError, ValueError) as exc:
            for leftover in (destination + ".part",):
                try:
                    if os.path.exists(leftover):
                        os.unlink(leftover)
                except OSError:
                    pass
            return False, f"{type(exc).__name__}: {exc}"


__all__ = ["ImageFactoryService", "PlanItem", "BatchResult", "PRESETS"]
