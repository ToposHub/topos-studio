# Files 替代 D3 批次验证记录（分页）

日期：2026-11-17。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) D 表第 3 项。

## 交付物

| 层 | 内容 |
| --- | --- |
| transport | `load_directory(path, offset=0)`；新信号 `page_meta(path,total,truncated)` 先于 `loaded` 送达；`PAGE_SIZE=500`（续页不 bump 导航代际——导航才作废） |
| 模型 | `FilesEntryModel.append_entries`（beginInsertRows 增量插入，不重置模型） |
| 面板 | `set_page_meta`（晚到的旧目录不覆盖）/`show_directory_page`（首页重建、续页追加）/`_load_more`（offset=已消费条数）；目录与全库搜索共用「加载更多（还有 N 项）」按钮；导航/`set_loading` 重置分页状态；搜索续页走 `show_library_results` 追加分支（不重建虚拟视图） |
| 接线 | FilesPanel：`directory_more_requested → load_directory(offset)`、`page_meta → set_page_meta`、`loaded → show_directory_page` |

## 语义决定

- **offset 分页**：`QueryService.list_directory(limit, offset)` 的 total 全量计数、
  truncated=`offset+len<total`；cursor=已消费条数（scandir 顺序，页间文件系统
  变化如实反映为漂移，不假装稳定快照）。
- **排序一致**：追加页经同一 QSortFilterProxyModel 全量重排，与首页同一比较器。
- **选择/滚动/缩略图身份**：续页不重置模型——选择保留；Entry 按 path 身份去重
  合并；缩略图键仍为 (path, version)。
- **防抖旧结果写权**：FR-09 防抖（最后一次输入才查询）+ facade 同键作废 +
  导航代际，三者叠加；新查询重建视图（测试覆盖）。
- 集合视图单发全量（无 truncated），不显示加载更多。

## 验证

- `tests/ui/test_files_pagination.py` **8 项**：transport 三页 3/3/1（total=7
  恒定、truncated 收尾为 False、跨页无重复）；续页不 bump 代际；面板首页→
  续页选择保持/按钮显隐/offset 语义/晚到 page_meta 不应用/导航重置；搜索
  首页+续页+终页；防抖后新查询重建视图。
- 受影响面：**347 + 8 + 88 passed**（D1/D2 全部 + 面板/布局/画廊等）。

## 诚实边界

- 虚拟列表未做（真分页已满足"真实分页**或**虚拟列表"的或关系；10k 项=20 页
  真实分页可承载，本地 proxy 排序无压力）。
- offset 分页的页间漂移（翻页期间增删文件）不检测不重排——如实按当次
  scandir 返回，下一页可能跳/重；这是 offset 语义的固有属性，未冒充 cursor
  稳定性。
- 搜索分页 limit 固定 200（面板既有值）；目录 500。1 万项目录实机验收属
  D 表第 9 项验收段。

## 隐私

仅源码/测试/Markdown；合成素材与临时目录。
