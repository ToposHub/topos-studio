# batch-305 — src/features/teaching_animation/

编号区间：AUD-30500–AUD-30599。审查日期：2026-09-28。

## src/features/teaching_animation/scene_cache.py (行数: 74)

### 发现
- [AUD-30501] [Major] 共享协调器惰性初始化无锁，多线程首次并发调用会创建多个 RenderWorker
  - 位置: src/features/teaching_animation/scene_cache.py:58-62
  - 证据:
    ```python
    def get_shared_scene_cache_coordinator() -> TeachingSceneCacheCoordinator:
        global _SHARED_COORDINATOR
        if _SHARED_COORDINATOR is None:
            _SHARED_COORDINATOR = TeachingSceneCacheCoordinator()
        return _SHARED_COORDINATOR
    ```
    （TeachingSceneCacheCoordinator.__init__ 会创建 RenderWorker，其内部启动 ThreadPoolExecutor，见 render/render_worker.py:53。）
  - 影响: check-then-set 竞态：两个线程同时通过 `is None` 判断会各建一个协调器，产生两份后台渲染线程池与两份 SceneRenderCache。schedule() 只取消自己 `_handles` 里的旧任务，另一实例的在途渲染不会失效，缓存重复渲染且 `invalidate` 只清一份，可能导致陈旧帧被继续命中。当前调用点（host_adapters.py:317/432、module.py:3371、motion_graphic_editor_window.py:1524/2979、timeline_preview_renderer.py:4100）多在 UI 层，实际触发概率低，但该单例被跨 features（edit/timeline_preview_renderer）消费，无法保证永远单线程初始化。
  - 建议: 用模块级 `threading.Lock` 双重检查，或在应用启动阶段（单线程）预先调用 get_shared_scene_cache_coordinator() 完成初始化。
  - 置信度: 中（竞态事实确定，实际触发路径未证实）

- [AUD-30502] [Suggestion] close() 在锁外调用 worker.shutdown()，与并发 schedule() 存在窗口；且全库未发现 close() 调用点
  - 位置: src/features/teaching_animation/scene_cache.py:49-52
  - 证据:
    ```python
    def close(self) -> None:
        self.worker.shutdown()
        with self._lock:
            self._handles.clear()
    ```
    （render_worker.py:80 shutdown 后 executor 拒绝新任务，此时再 submit 会抛 RuntimeError。）
  - 影响: close 与 schedule 并发时，schedule 可在 shutdown 之后向已关闭的 executor 提交任务并抛 RuntimeError。目前 src/ 下未发现任何 `coordinator.close()` 调用（该方法为死代码），风险尚未触发，但一旦接入关闭流程即暴露。
  - 建议: 在锁内先标记 closed 状态使 schedule 拒绝新任务，再执行 shutdown；或删除无调用方的 close() 以免误用。
  - 置信度: 高（竞态与"无调用方"均经 grep 核实）

- [AUD-30503] [Suggestion] schedule() 在持有 RLock 期间执行模块导入
  - 位置: src/features/teaching_animation/scene_cache.py:26-27
  - 证据:
    ```python
        with self._lock:
            previous = self._handles.get(scene.scene_id)
            ...
            from .render.scene_renderer_router import backend_for_scene
            backend = backend_for_scene(scene)
    ```
  - 影响: 持自定义锁做 import（解释器级 import 锁参与），若其他线程同时导入依赖链回环的模块，理论上有锁顺序交叉死锁风险；同时把首帧调度延迟绑到导入耗时上。render_worker.submit 内部同样的导入（render_worker.py:60）在锁外，此处不必要。
  - 建议: 将 backend_for_scene 的导入移到锁外或模块顶部（若为避免循环导入，可在 schedule 入口先取 backend 再加锁）。
  - 置信度: 低（死锁为理论路径，未构造出实际用例）

### 通过项
- GPU 资源生命周期：协调器本身不直接持有 ModernGL 对象，渲染资源由 RenderWorker/渲染器在 finally 中清理（render_worker.py:119-120 renderer.cleanup()）。
- 状态提交一致性：schedule() 对同 scene_id 的未完成任务先 `previous.cancel()` 再提交新任务，替换语义正确；`allow_uniform_frames=backend == "html"` 的放宽有注释说明仅限非权威投递缓存，不影响导出/校验门禁。
- 异常与取消路径：RenderHandle.cancel() 同时取消 token 与 future；cancel 后 `_render` 在每帧循环开头检查 token（render_worker.py:95-96）。
- 输入验证：invalidate 仅转发 cache.invalidate_scene；schedule 信任 TeachingSceneDocument（内部模型数据），边界可接受。
- 性能模式：无逐帧热路径逻辑。
- 类型注解：完整（schedule/invalidate/close 均有注解）。

## src/features/teaching_animation/scene_store.py (行数: 97)

### 发现
- [AUD-30504] [Major] replace_all()/clear() 清空 revision 计数后从 1 重新计数，跨项目切换可产生与历史相同编号的 revision
  - 位置: src/features/teaching_animation/scene_store.py:66-71, 84-87, 54
  - 证据:
    ```python
        def replace_all(self, scenes: Iterable[TeachingSceneDocument]) -> None:
            with self._lock:
                self._scenes.clear()
                self._revisions.clear()
            for scene in scenes:
                self.register(scene)
    ...
                self._revisions[scene.scene_id] = self._revisions.get(scene.scene_id, 0) + 1
    ```
    消费方：host_adapters.py:201 `render_cache_id=f"scene:{self.scene.scene_id}:r{self.scene_store.revision(self.scene.scene_id)}"`；module.py:2181 `if current is None or store.revision(current.scene_id) != plan["scene_revision"]:`
  - 影响: 项目关闭走 `replace_all(())`（app/controllers/file_controller.py:1278），打开新项目再注册同 scene_id（场景 id 来自工程文件，可重复出现）时 revision 从 1 重新计数。内存缓存键 `scene:<id>:r1` 可能命中上一项目同一 id 早期 revision 的陈旧渲染结果；module.py 的 scene_revision 相等性判断在数值跨越重置边界后碰撞时误判"无需刷新"。
  - 建议: revision 改为进程级单调计数器（全局原子递增），或 replace_all/clear 时不清空计数、仅在 id 消亡后归零前记录代次（generation）。
  - 置信度: 中（重置事实与消费方引用已核实；具体陈旧命中链路未实测）

- [AUD-30505] [Suggestion] remove()/clear()/replace_all(()) 不通知订阅者，删除事件对宿主不可见
  - 位置: src/features/teaching_animation/scene_store.py:60-64, 66-71, 84-87
  - 证据:
    ```python
        def remove(self, scene_id: str) -> TeachingSceneDocument | None:
            with self._lock:
                removed = self._scenes.pop(scene_id, None)
                self._revisions.pop(scene_id, None)
                return removed
    ```
    （仅 register() 在 55-57 行通知 listeners；remove/clear/replace_all 无任何 listener 调用。）
  - 影响: 订阅方（composite_node.py:51 `subscribe(self._scene_changed)`、host_adapters.py:257 `subscribe(self.register_scene)`）只感知新增/更新。项目关闭（replace_all(())）后组合节点持有的 scene_id 已失效但节点 state 不会被标脏；当前 composite_node._do_evaluate 中 `get()` 返回 None 走空输出（fail-soft），故后果是静默空帧而非崩溃。
  - 建议: 为 store 增加 on_remove 通知（或在 clear/replace_all 时以空/墓碑事件通知订阅者），让宿主能主动解绑失效 scene_id。
  - 置信度: 高（行为与调用方均已核实）

- [AUD-30506] [Suggestion] SceneChange 数据类定义后从未被使用（死代码）
  - 位置: src/features/teaching_animation/scene_store.py:20-23
  - 证据:
    ```python
    @dataclass(frozen=True)
    class SceneChange:
        scene: TeachingSceneDocument
        revision: int
    ```
    （全库 grep 仅见本文件定义与 __all__ 导出；listeners 实际收到的是裸 TeachingSceneDocument。）
  - 影响: 误导读者以为订阅回调携带 revision；维护成本。
  - 建议: 要么让 register 通知时构造 SceneChange（携带 revision，还能顺带缓解 AUD-30504 的判别问题），要么删除该类及其 __all__ 条目。
  - 置信度: 高

- [AUD-30507] [Suggestion] replace_all() 的"清空+逐个重注册"不是原子操作，中间态对并发读者可见
  - 位置: src/features/teaching_animation/scene_store.py:66-71
  - 证据:
    ```python
        def replace_all(self, scenes: Iterable[TeachingSceneDocument]) -> None:
            with self._lock:
                self._scenes.clear()
                self._revisions.clear()
            for scene in scenes:
                self.register(scene)
    ```
  - 影响: clear 与各 register 分别加锁，之间释放锁。并发线程在窗口内调用 scenes()/get() 会看到空或部分填充的仓库；例如渲染线程 get() 短暂返回 None 触发空输出。当前调用方 file_controller 在 UI 线程做项目打开，冲突概率低，但与类 docstring "Thread-safe scene repository" 的承诺不完全一致。
  - 建议: 在单次持锁内完成 clear+重建（listeners 快照后在锁外统一通知），保证外部观察要么全旧要么全新。
  - 置信度: 中

### 通过项
- 线程安全（读路径）：get/scenes/revision/subscribe/unsubscribe 均在 RLock 内完成；register 在锁外通知 listeners（快照 tuple），避免持锁回调死锁，且重入（listener 内再 register/subscribe）由 RLock 与快照共同保证安全。
- 信号防递归：Qt-free 设计，无信号；register 的 `previous == scene` 短路可防止相同文档重复通知（TeachingSceneDocument/SceneElement 为 frozen dataclass，按值比较）。
- 异常与取消路径：无可吞异常的宽泛 except；listener 异常会传播给 register 调用方（符合不掩盖领域错误的铁律）。
- 输入验证：register 依赖 TeachingSceneDocument 自身 __post_init__ 校验（model.py 中 element/type/id 校验）。
- 性能模式：读操作 O(1)/O(n) 拷贝快照，无热点问题。
- 类型注解：完整（含 Iterable/Callable 签名与返回类型）。
- 持久化：store 为进程内状态，序列化由 Project IO 负责，职责边界清晰。
