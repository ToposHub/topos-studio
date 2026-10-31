# 批次报告：batch-329 [P2] src/shared/db//

编号区间：AUD-32900–AUD-32999
审查文件：2 个（`src/shared/db/__init__.py`、`src/shared/db/database.py`）
本模块为 SQLite 持久化层（settings / layouts / shortcuts / file_meta），不涉及 GPU、GLSL、Qt 信号。

## src/shared/db/__init__.py (行数: 2)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export，仅 `from src.shared.db.database import Database` 并声明 `__all__ = ["Database"]`。

## src/shared/db/database.py (行数: 239)

### 发现

- [AUD-32901] [Major] 排序热路径上每次调用新建/关闭 SQLite 连接，无复用无批量接口
  - 位置: src/shared/db/database.py:110-116（另 118-126 同模式）
  - 证据:
    ```python
    def get_file_rating(self, path):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT rating FROM file_meta WHERE path = ?', (_meta_key(path),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    ```
    调用方 `src/app/ui/library_panel.py:334-337`（LibraryProxyModel.lessThan，按评分排序时每次比较调用两次 get_file_rating）：
    ```python
    if self.sort_criteria == "rating":
        r1 = self.db.get_file_rating(left_path)
        r2 = self.db.get_file_rating(right_path)
        return r1 < r2
    ```
  - 影响: Qt 排序 O(n log n) 次比较 × 每次比较 2 次连接建立/关闭；大目录（数千文件）一次"按评分排序"触发数万次 connect+close，全部发生在 UI 线程，造成明显卡顿。
  - 建议: 提供批量读取接口（如按目录一次性载入 path→rating 的 dict 供排序缓存），或复用单一长连接；调用方在排序前预取评分映射。
  - 置信度: 高

- [AUD-32902] [Major] layouts 单主键→复合主键迁移非原子，崩溃窗口内可永久丢失全部布局预设
  - 位置: src/shared/db/database.py:54-84
  - 证据:
    ```python
    cursor.execute('ALTER TABLE layouts RENAME TO layouts_legacy')
    cursor.execute('''CREATE TABLE layouts (...)''')
    ...
    cursor.execute('''INSERT INTO layouts (...) SELECT ... FROM layouts_legacy''')
    cursor.execute('DROP TABLE layouts_legacy')
    ```
  - 影响: Python sqlite3 默认 isolation_level 只对 DML 隐式开事务，RENAME/CREATE 等 DDL 在无打开事务时各自即时提交。若进程在 RENAME 提交后、回填完成前崩溃，下次启动 `CREATE TABLE IF NOT EXISTS layouts`（新 schema）直接建出空表，`PRAGMA table_info` 检测到 work_mode 列存在即跳过迁移，`layouts_legacy` 永久成为孤儿表——用户全部布局预设静默丢失。
  - 建议: 用 `BEGIN IMMEDIATE` 显式包裹整个迁移（RENAME 到 DROP），或在检测到遗留 `layouts_legacy` 表时先行恢复/合并，再进入正常建表流程。
  - 置信度: 中（依赖 sqlite3 对 DDL 的事务处理细节，但代码未显式开启事务是事实）

- [AUD-32903] [Major] 全部方法无 try/finally，异常路径连接泄漏且异常直透 UI 槽
  - 位置: src/shared/db/database.py:110-126、128-141、143-239（全部读写方法同模式）
  - 证据:
    ```python
    conn = sqlite3.connect(self.db_path)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO file_meta (path, rating) VALUES (?, ?)
        ON CONFLICT(path) DO UPDATE SET rating = excluded.rating
    ''', (_meta_key(path), rating))
    conn.commit()
    conn.close()
    ```
  - 影响: 文件头注释明确 topos.db 与 file_explorer/Flask 后端共享（`使用 file_meta 与 Flask 后端保持一致`）。外部进程持写锁超过默认 5 秒 busy timeout 时，`execute` 抛 `OperationalError: database is locked`：`conn.close()` 不执行（连接及其持有的锁存活至 GC），异常从 UI 槽（如 library_panel.py:286/307 的评分点击）冒泡——PyQt6 未捕获的槽异常默认会终止程序。
  - 建议: 所有方法改用 `with sqlite3.connect(self.db_path) as conn:`（或 try/finally 关闭），并在持久化层为 `OperationalError` 提供重试/超时参数（如 `timeout=`、启用 WAL），避免瞬时锁竞争演变为崩溃。
  - 置信度: 中（锁竞争依赖外部进程行为，但无任何异常防护是事实）

- [AUD-32904] [Suggestion] json.loads 直接解析持久化字段，无损坏数据防御
  - 位置: src/shared/db/database.py:175、214
  - 证据:
    ```python
    if row:
        panel_states = json.loads(row[2]) if row[2] else None
        return (row[0], row[1], panel_states)
    ```
  - 影响: `panel_states` 为损坏/非 JSON 内容时 `load_layout`/`get_default_layout` 抛 `JSONDecodeError`；`load_layout` 由菜单动作直接调用（dock_layout_controller.py:2455），异常无回退路径。SQLite 提交原子性使概率较低，但违反"外部数据须验证"铁律。
  - 建议: `json.loads` 包 try/except，失败时按 None 处理并记录警告。
  - 置信度: 高（代码路径确定，触发概率低）

- [AUD-32905] [Suggestion] get_file_rating 对 NULL rating 返回 None 而非 0
  - 位置: src/shared/db/database.py:116
  - 证据:
    ```python
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0
    ```
  - 影响: `file_meta.rating` 列定义为 `INTEGER DEFAULT 0` 但允许 NULL，且该表与外部 file_explorer/Flask 端共写；若外部写入显式 NULL，行存在时返回 None，调用方 `lessThan`（library_panel.py:337 `return r1 < r2`）将抛 TypeError 使排序失败。守卫只覆盖"无行"，未覆盖"值为 NULL"。
  - 建议: `return row[0] if row and row[0] is not None else 0`，或将列收紧为 `NOT NULL DEFAULT 0`（需迁移）。
  - 置信度: 中（依赖外部写入行为）

- [AUD-32906] [Suggestion] 内置 'Default' 预设名与用户预设名共享主键，互相覆盖（潜在）
  - 位置: src/shared/db/database.py:229-237（save_default_layout 硬编码 `'Default'`）与 154-157（save_layout `INSERT OR REPLACE`）
  - 证据:
    ```python
    cursor.execute('''
        INSERT INTO layouts (name, geometry, state, panel_states, is_default, work_mode)
        VALUES ('Default', ?, ?, ?, 1, ?)
        ON CONFLICT(name, work_mode) DO UPDATE SET ...
    ```
  - 影响: 用户在保存对话框（dock_layout_controller.py:2371-2376，无名称校验）输入 "Default" 时，`INSERT OR REPLACE` 会覆盖默认布局行并把 is_default 写为 0；反之 `save_default_layout` 会覆盖用户同名预设。当前 `save_default_layout`/`get_default_layout` 在 src/ 内无调用方，属潜在风险。
  - 建议: 为内置默认行使用保留前缀（如 `__default__`），或在 save_layout/save_default_layout 入口拒绝/重命名冲突的 "Default"。
  - 置信度: 高（代码层面）；当前无触发路径

- [AUD-32907] [Suggestion] QByteArray 导入未使用；set_setting 对任意对象盲目 str()
  - 位置: src/shared/db/database.py:5、139
  - 证据:
    ```python
    from PyQt6.QtCore import QByteArray   # 全文件唯一出现，从未使用
    ...
    cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
    ```
  - 影响: 死导入（应为早期直接存 QByteArray 的残留）且引入对 PyQt6 的不必要依赖；`str(value)` 对 QByteArray 等对象会写入 `PyQt6.QtCore.QByteArray(b'...')` 形式的不可逆垃圾字符串，读回的 get_setting 无法还原。
  - 建议: 删除未用导入；set_setting 收紧为只接受 str/int/float 并显式转换，非标量类型抛 TypeError。
  - 置信度: 高

- [AUD-32908] [Suggestion] 全文件无类型注解
  - 位置: src/shared/db/database.py:8-239（所有函数与方法）
  - 证据:
    ```python
    def __init__(self, db_path="topos.db"):
    def save_layout(self, name, geometry, state, panel_states=None, is_default=False, work_mode="all"):
    def load_layout(self, name, work_mode="all"):
    ```
  - 影响: 与项目"强类型（所有函数有类型注解）"规范不符；`db_path`、`rating`、返回元组等契约只能靠 docstring 猜测。
  - 建议: 补齐参数与返回类型（如 `def load_layout(self, name: str, work_mode: str = "all") -> tuple[Optional[str], Optional[str], Optional[dict]]`）。
  - 置信度: 高

### 通过项
- 线程安全：每方法独立短生命周期连接（创建与使用同线程），无跨线程共享连接；当前全部调用点（main_window、library_panel、dock/file controller）均在 UI 线程。跨进程并发风险已在 AUD-32903 记录。
- GPU 资源生命周期：本模块不涉及 OpenGL/ModernGL 资源。
- GLSL·HDR 正确性：不涉及。
- 信号防递归：不涉及 Qt 信号。
- 状态提交一致性：所有写方法均即时 `conn.commit()`；`save_default_layout` 同事务内先清旧默认再置新默认，单一 work_mode 内默认唯一性有保障；`_meta_key` NFC 归一在 file_meta 读写两侧（get/set_file_rating）一致应用。
- 输入验证：`_meta_key` 对 None/空串安全；`set_file_rating` 评分范围未校验但现有调用方均传 0-5（次要，未单列）。
