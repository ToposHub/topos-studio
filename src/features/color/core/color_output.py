"""
ColorOutput - Represents a Color pipeline result at a specified resolution.

Decoupled from the Canvas viewport. Not affected by zoom/pan.
Supports proxy mode (for interactive preview) and full mode (for export).

Lifecycle:
- Created by ImageEngine alongside the Color pipeline
- Marked dirty when Color parameters change
- Re-rendered when entering Composite page (or on demand)
- Consumed by the COLOR node in the Composite graph

Threading contract (AUD-0301):
- The canonical writer is ImageEngine's ``_on_color_output_ready`` slot,
  which runs on the ImageEngine thread via a queued Qt signal; readers on
  other threads must use :meth:`snapshot` to observe a consistent
  (texture_id, width, height, revision) tuple. Direct property reads are
  only guaranteed consistent on the writing thread.
"""


class ColorOutput:
    """Manages a cached Color pipeline result at a controlled resolution."""

    # Maximum dimension for proxy mode (good balance of quality vs performance)
    PROXY_MAX_DIM = 2048

    def __init__(self) -> None:
        """Initialize an empty, dirty ColorOutput with no cached texture."""
        # AUD-0301: (texture_id, width, height, revision) 存成单个不可变元组，
        # update() 用一次赋值整体替换，读端经 snapshot() 拿到一致快照，
        # 不会出现"新 texture_id 配旧尺寸"的撕裂组合。
        self._state: tuple[int | None, int, int, int] = (None, 0, 0, 0)
        self._dirty: bool = True

    @property
    def texture_id(self) -> int | None:
        """OpenGL texture ID of the cached Color result."""
        return self._state[0]

    @property
    def width(self) -> int:
        """Width of the cached texture in pixels."""
        return self._state[1]

    @property
    def height(self) -> int:
        """Height of the cached texture in pixels."""
        return self._state[2]

    @property
    def revision(self) -> int:
        """Revision counter — incremented on each update()."""
        return self._state[3]

    @property
    def is_dirty(self) -> bool:
        """Whether the ColorOutput needs to be re-rendered."""
        return self._dirty

    @property
    def is_valid(self) -> bool:
        """Whether a usable texture is available (even if stale)."""
        return self._state[0] is not None and self._state[0] > 0

    def snapshot(self) -> tuple[int | None, int, int, int]:
        """Return a consistent (texture_id, width, height, revision) snapshot.

        AUD-0301: 跨线程消费方应使用本方法而不是逐个属性读取，保证四个
        字段来自同一次 update()（元组整体替换，无撕裂读）。
        """
        return self._state

    def mark_dirty(self) -> None:
        """Mark the ColorOutput as needing re-render."""
        self._dirty = True

    def update(self, texture_id: int, width: int, height: int) -> None:
        """Update the cached ColorOutput with a new render result."""
        # AUD-0301: 单次元组赋值整体替换四个字段（CPython 下原子），
        # 读者要么看到旧结果要么看到新结果，不会读到中间组合。
        self._state = (texture_id, width, height, self._state[3] + 1)
        self._dirty = False

    @staticmethod
    def compute_proxy_size(orig_w: int, orig_h: int,
                           max_dim: int | None = None) -> tuple[int, int]:
        """Compute proxy dimensions maintaining aspect ratio.

        Args:
            orig_w: Original image width
            orig_h: Original image height
            max_dim: Maximum dimension (defaults to PROXY_MAX_DIM)

        Returns:
            (target_w, target_h) tuple
        """
        if max_dim is None:
            max_dim = ColorOutput.PROXY_MAX_DIM
        if orig_w <= max_dim and orig_h <= max_dim:
            return orig_w, orig_h
        scale = min(max_dim / orig_w, max_dim / orig_h)
        return int(orig_w * scale), int(orig_h * scale)
