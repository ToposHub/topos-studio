"""调色会话撤销/重做历史（HistoryManager）。

职责：维护两个快照栈（undo/redo），每个快照封存某一时刻的完整编辑
状态——图像像素、全部图层的序列化状态、活动图层索引与翻转标志。
undo()/redo() 采用"当前状态换历史状态"的对称交换语义，新操作
（push_state）会清空重做栈。

## 快照不可变性契约（核心约定）

- **入栈即冻结**：``push_state``/``undo``/``redo`` 存入栈中的 state
  dict 一经写入即归 HistoryManager 所有，任何调用方不得就地修改栈内
  快照；栈内数据只被整体弹出（pop）或清空（clear），从不原地改写。
- **图像快照**：默认 ``copy_image=True`` 时入栈为像素副本；非破坏性
  调色路径传 ``copy_image=False`` 保留源帧引用——此时调用方必须保证
  该 ndarray 之后绝不被就地修改（源帧像素按约定不可变），否则所有
  引用同一数组的历史状态将一同被污染。
- **图层快照**：经由 ``Layer.to_dict`` 序列化后由 ``_serialize_layers``
  整体 deepcopy 冻结（``to_dict`` 对嵌套可变容器按引用返回），栈内与
  活动 Layer 对象互不共享可变状态；恢复时用 ``deserialize_layers``
  重建全新 Layer 实例。
- **出栈即移交**：undo()/redo() 返回的 dict 已从栈中弹出，所有权
  移交调用方，可自由使用。

线程边界：无锁、非线程安全；仅在 UI 主线程访问（由 ColorEngine /
面板在用户操作处调用），渲染与导出线程不触碰本模块。
"""
import copy
from typing import List, Dict, Any, Optional, Callable
import numpy as np


class HistoryManager:
    """
    管理撤销/重做历史记录，支持保存图像数据和图层状态快照。

    每个历史状态包含：
    - 图像数据 (numpy array)
    - 所有图层的序列化状态
    - 当前活动图层索引

    遵循模块级"快照不可变性契约"：入栈快照冻结为管理器所有，
    出栈快照所有权移交调用方。
    """

    def __init__(self, max_steps: int = 100) -> None:
        """
        创建空历史管理器。

        Args:
            max_steps: 撤销栈容量上限；超出后丢弃最旧状态
                （重做栈不设上限，但在每次 push_state 时被清空）。
        """
        self.max_steps = max_steps
        self.undo_stack: List[Dict[str, Any]] = []
        self.redo_stack: List[Dict[str, Any]] = []
        # Optional session state follows every snapshot, including the current
        # state captured by undo/redo. Existing callers need no new arguments.
        self.extra_state_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.extra_state_restorer: Optional[Callable[[Dict[str, Any]], None]] = None

    def _capture_extra_state(self, state: Dict[str, Any]) -> None:
        """Freeze additional session data without overriding core fields."""
        if self.extra_state_provider is None:
            return
        extra = self.extra_state_provider()
        if state.keys() & extra.keys():
            raise ValueError("History extra state must not replace core snapshot fields")
        state.update(copy.deepcopy(extra))

    def restore_extra_state(self, state: Dict[str, Any]) -> None:
        """Restore optional session data when the owner applies a snapshot."""
        if self.extra_state_restorer is not None:
            self.extra_state_restorer(state)

    def push_state(self, image: np.ndarray, layers: Optional[List] = None, active_layer_index: int = 0,
                   flip_h: Optional[bool] = None, flip_v: Optional[bool] = None,
                   copy_image: bool = True,
                   crop_params: Optional[Dict[str, Any]] = None) -> None:
        """
        保存状态到撤销栈。

        Args:
            image: 当前图像数据 (img_full_original)
            layers: 图层列表 (Layer 对象列表)
            active_layer_index: 当前活动图层索引
            flip_h: 当前水平翻转状态；None 表示本快照不跟踪翻转
                （BUG-A2-01：恢复时保持引擎现行标志，兼容未记录翻转
                的调用方——crop/resize 等操作不改变翻转态）
            flip_v: 当前垂直翻转状态；None 语义同 flip_h
            copy_image: Whether to deep-copy the source pixels.  Non-
                destructive grading only changes serialized layer state, so it
                can safely retain the immutable source-frame reference.
            crop_params: 当前裁剪参数（AUD-0602：flip 重建从原图重放
                crop_params，undo/redo 必须随快照恢复该参数，否则撤销
                裁剪后再次翻转会用最新裁剪参数重建出错误画面）。
                None 表示本快照不跟踪裁剪参数（恢复时保持现行值）。

        入栈后快照冻结为管理器所有（见模块级不可变性契约）；本操作
        同时清空重做栈。
        """
        if image is None:
            return

        # 清空重做栈（执行新操作时）
        self.redo_stack.clear()

        # 创建状态快照
        state = {
            'image': image.copy() if copy_image else image,
            'layers': self._serialize_layers(layers) if layers else [],
            'active_layer_index': active_layer_index,
            'flip_h': flip_h,
            'flip_v': flip_v,
            'crop_params': copy.deepcopy(crop_params) if crop_params is not None else None,
        }

        self._capture_extra_state(state)
        self.undo_stack.append(state)

        # 限制栈大小
        if len(self.undo_stack) > self.max_steps:
            self.undo_stack.pop(0)

    def undo(self, current_image: np.ndarray, current_layers: Optional[List] = None,
             current_active_index: int = 0,
             current_flip_h: Optional[bool] = None,
             current_flip_v: Optional[bool] = None,
             current_crop_params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        撤销到上一个状态，保存当前状态到重做栈。

        Args:
            current_image: 当前图像数据
            current_layers: 当前图层列表
            current_active_index: 当前活动图层索引
            current_flip_h: 当前水平翻转状态（BUG-A2-01：对称交换语义下
                重做栈必须记录翻转，否则 redo 回来时翻转态丢失）
            current_flip_v: 当前垂直翻转状态
            current_crop_params: 当前裁剪参数（AUD-0602：对称交换语义下
                重做栈必须记录，否则 redo 后 flip 重放用错裁剪参数）

        Returns:
            上一个状态的字典，包含 'image', 'layers', 'active_layer_index',
            'flip_h', 'flip_v', 'crop_params'；如果撤销栈为空则返回 None

        返回的快照已弹出、所有权移交调用方；当前状态被深拷贝存入
        重做栈。
        """
        if not self.undo_stack:
            return None

        # 保存当前状态到重做栈
        if current_image is not None:
            redo_state = {
                'image': current_image.copy(),
                'layers': self._serialize_layers(current_layers) if current_layers else [],
                'active_layer_index': current_active_index,
                'flip_h': current_flip_h,
                'flip_v': current_flip_v,
                'crop_params': (copy.deepcopy(current_crop_params)
                                if current_crop_params is not None else None),
            }
            self._capture_extra_state(redo_state)
            self.redo_stack.append(redo_state)

        return self.undo_stack.pop()

    def redo(self, current_image: np.ndarray, current_layers: List = None,
             current_active_index: int = 0,
             current_flip_h: Optional[bool] = None,
             current_flip_v: Optional[bool] = None,
             current_crop_params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        重做到下一个状态，保存当前状态到撤销栈。

        Args:
            current_image: 当前图像数据
            current_layers: 当前图层列表
            current_active_index: 当前活动图层索引
            current_flip_h: 当前水平翻转状态（BUG-A2-01：对称交换语义下
                撤销栈必须记录翻转，否则再次 undo 回来时翻转态丢失）
            current_flip_v: 当前垂直翻转状态
            current_crop_params: 当前裁剪参数（AUD-0602：对称交换语义下
                撤销栈必须记录）

        Returns:
            下一个状态的字典，包含 'image', 'layers', 'active_layer_index',
            'flip_h', 'flip_v', 'crop_params'；如果重做栈为空则返回 None

        返回的快照已弹出、所有权移交调用方；当前状态被深拷贝存入
        撤销栈。
        """
        if not self.redo_stack:
            return None

        # 保存当前状态到撤销栈
        if current_image is not None:
            undo_state = {
                'image': current_image.copy(),
                'layers': self._serialize_layers(current_layers) if current_layers else [],
                'active_layer_index': current_active_index,
                'flip_h': current_flip_h,
                'flip_v': current_flip_v,
                'crop_params': (copy.deepcopy(current_crop_params)
                                if current_crop_params is not None else None),
            }
            self._capture_extra_state(undo_state)
            self.undo_stack.append(undo_state)

        return self.redo_stack.pop()

    def can_undo(self) -> bool:
        """是否存在可撤销状态。

        Returns:
            bool: True 表示 undo() 会返回快照而非 None。
        """
        return len(self.undo_stack) > 0

    def can_redo(self) -> bool:
        """是否存在可重做状态。

        Returns:
            bool: True 表示 redo() 会返回快照而非 None。
        """
        return len(self.redo_stack) > 0

    def clear(self) -> None:
        """清空撤销与重做栈（新素材加载/工程重置时调用）。"""
        self.undo_stack.clear()
        self.redo_stack.clear()

    def _serialize_layers(self, layers: List) -> List[Dict[str, Any]]:
        """
        序列化图层列表为可存储的字典列表。

        Args:
            layers: Layer 对象列表

        Returns:
            序列化后的字典列表
        """
        serialized = []
        for layer in layers:
            # AUD-0501：Layer.to_dict / MaskShape.to_dict 对 grading_params、
            # plugins、points 等可变容器按引用序列化，浅快照会被压栈后的
            # 就地编辑污染（undo/redo 静默失效）。入栈前整体 deepcopy 冻结。
            serialized.append(copy.deepcopy(layer.to_dict(assets_dir=None)))
        return serialized

    @staticmethod
    def deserialize_layers(layers_data: List[Dict[str, Any]]) -> List:
        """
        反序列化字典列表为 Layer 对象列表。
        
        Args:
            layers_data: 序列化的图层数据列表
            
        Returns:
            Layer 对象列表
        """
        from src.features.color.core.layer import Layer
        layers = []
        for layer_data in layers_data:
            layer = Layer.from_dict(layer_data, assets_dir=None)
            layers.append(layer)
        return layers
