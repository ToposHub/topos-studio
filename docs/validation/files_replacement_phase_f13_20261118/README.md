# F 阶段第十三批（F13）：按实际目标平台的安装锁——无 fcntl 等价原语

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/install_lock.py`
（新）+ `_Transaction` 换轨。此前 ``fcntl`` 缺失即 ``LOCK_UNAVAILABLE``
——把"拒绝安装"当成跨平台支持完成（F13 明令禁止的反模式）。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `install_lock.py`（新） | ① **`_FlockLock`**：POSIX flock 版（从 `_Transaction` 抽出，语义不变：独占、阻塞、内核在进程退出含 kill -9 时自动释放）。② **`_MsvcrtLock`**：Windows `msvcrt.locking(LK_NBLCK)` 重试循环——阻塞语义等价；句柄关闭/进程退出由 OS 释放（与 flock 一致）；`stall_timeout` 可选上限。③ **`create_lock`** 按实际平台选择；**两者皆无 → `LockUnavailable`**——对不支持平台的诚实拒绝（"该平台不支持并发安装事务，拒绝执行"，非把拒绝冒充支持）。④ **`HeldTransactionLock`**：线程锁 + 跨进程锁组合持有（同进程串行 + 跨进程互斥），`_Transaction` 换用（异常映射 LOCK_UNAVAILABLE/PATH_NOT_OWNED 保持） |

## 语义一致性验证（tests/plugins/test_install_lock_f13.py，10 项）

- **POSIX 真跨进程**（本机真子进程 + 真锁文件）：
  - 独占/阻塞——持有者未放行时竞争者仍在等（poll None），释放后
    HELD→RELEASED 依序；
  - **异常退出**——持有者 `kill -9` 后竞争者无需人工清理即获得
    （内核自动释放）；
  - **安装事务换轨**——独立进程持锁期间，另一进程的
    `installer.install()` 线程等待（outcomes 空），释放后完成安装
    （真跨进程互斥走完整安装路径）。
- **Windows msvcrt 等价**（替身锁定语义）：LK_NBLCK 失败→重试循环→
  释放后获得；release/close 幂等；模块缺失→诚实拒绝。
- 平台选择/可用性事实/双缺失消息/组合持有重入。

回归：tests/plugins 全量（含 installer/TOCTOU/lifecycle/provider/sandbox）
—— **469 通过**。

## 诚实边界

- **Windows 真机未跑**：msvcrt 路径以等价替身锁定逻辑（重试/释放/幂等）；
  真机验收归平台矩阵（本开发机为 macOS）。POSIX 侧全部真进程验证。
- 阻塞等待无公平性保证（flock/msvcrt 均内核调度）；安装场景单写者
  足够。
- 线程级仍为 RLock 串行（同进程多线程排队）；跨进程语义如上。

## 隐私

真子进程 + 临时锁文件/合成包；无隐私路径。
