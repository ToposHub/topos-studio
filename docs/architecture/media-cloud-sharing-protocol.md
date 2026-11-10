# Cloud / Power Bins / 跨工程共享——协议设计（P2-1）

状态：**设计稿，未实现**。对应《媒体池后端交接计划》P2 第 1 项：在具备
真实服务之前，**不接任何 UI、不落任何假成功入口**（能力声明见文末）。
本文固化身份/同步/冲突/权限/离线/配额六域协议，供未来服务实施时按此验收。

## 1. 身份（Identity）

| 层 | 键 | 说明 |
|---|---|---|
| 共享箱（Power Bin） | `power_bin_id: UUIDv7` | 服务端权威分配；本地不得自行生成（离线新建走 §5 临时 ID，同步时置换） |
| 共享箱内容引用 | `(power_bin_id, asset_ref_id)` | 引用式（对齐 P0-5 的素材箱引用语义）；asset 本体不复制进箱 |
| 媒体身份 | `media_identity = (size_bytes, mtime_ns, sha256_head)` | 与既有 relink 三级匹配（名→大小→内容短哈希）同源；云端去重键 |
| 工程引用 | `(project_id, power_bin_id, pinned_revision)` | 工程记录"当时使用的箱修订"，重开时据此判定是否需要重解析 |

## 2. 同步（Sync）

- 拉取模型：**快照 + 增量**。`GET /power-bins/{id}/snapshot?rev=N` 返回
  `rev=N` 全量清单（媒体引用 + 日志字段 + 规则树）；`?since=N` 返回
  变更日志（append-only，服务端保留 ≥90 天）。
- 推送模型：**命令日志**。客户端把本地对共享箱的修改编码为命令
  （复用 `ProjectCommandStack` 的 command 序列化契约），服务端按收到的
  顺序重放；服务端是唯一修订号发放者。
- 上行带宽策略：媒体文件按 `media_identity` 内容寻址（已有对象跳传）。

## 3. 版本冲突（Conflicts）

- 检测：客户端命令重放时目标 `rev` 与服务端当前 `rev` 不一致 → 冲突。
- 解决策略（字段级三方合并，base=两端共同祖先 rev）：
  1. **可自动合并**：不同素材的命令、同素材不同字段（对齐
     `merge_replacement_metadata` 的键域白名单思路）；
  2. **需要人工**：同字段不同值 → 生成冲突副本（`副本 @rev{N}` 保留
     两值 + 冲突标记素材，出现在箱顶部的"冲突"分区）；
  3. **绝不**静默取任一侧——冲突副本可被用户显式解决（选择/合并/丢弃），
     解决动作本身是一条新命令。
- 撤权/删除的素材引用：本地已用该引用的**时间线不受影响**（引用是
  值拷贝），仅箱内条目标记 `unavailable`，重连后给出诊断。

## 4. 访问权限（Permissions）

- 三级：`read`（拉取快照）/ `write`（提交命令）/ `owner`（转让/撤权）。
- 权限校验在**服务端**执行；客户端缓存权限仅用于 UI 置灰，不作为
  安全边界。令牌走系统钥匙串，**绝不入 `.topos` 工程**（对齐"工程不存
  secret"红线）。
- 撤权（revoked）：拉取返回 403 → 本地箱转只读快照（`revoked` 态，
  内容仍可浏览/复制出箱），提交队列清空并提示；不删除本地数据。

## 5. 离线缓存（Offline Cache）

- 本地缓存目录：`<workspace>/cloud-cache/<power_bin_id>/<rev>/`
  （清单 JSON + 已拉取媒体的临时副本；**不写机器绝对路径进工程**，
  工程只存 `power_bin_id + pinned_revision`，路径由运行时解析——
  对齐"不保存不可重建大缓存"红线）。
- 断网：进入离线态——只读浏览已缓存 rev；允许离线新建**本地箱**
  （普通 `ProjectBin`），不支持离线改共享箱（命令队列不落盘，避免
  断网期间的命令在重连后与服务端状态错位）。
- 重连：先 `snapshot` 校准 rev，再进 §2/§3 流程。

## 6. 配额（Quota）

- 服务端按租户返回 `{storage_bytes, monthly_transfer_bytes,
  power_bin_count}` 与用量；客户端在**上传前**预检（按 media_identity
  求和，跳过云端已有对象），超限即拒绝并给出明细，不传一半失败。
- 本地不缓存配额数字超过一次会话（避免陈旧决策）。

## 7. 能力声明（Capability Registry）

实施前在 `src/features/edit/core/capability_registry.py` 登记：

```python
Capability(
    id="cloud_power_bins",
    title="Cloud / Power Bins（跨工程共享）",
    status=CapabilityStatus.UNAVAILABLE,   # 未实现，绝不虚报
    evidence=CapabilityEvidence(
        implementation=(),                 # 无实现文件
        tests=(),                          # 无测试
        notes="协议设计见 docs/architecture/media-cloud-sharing-protocol.md；"
              "具备云服务后按协议实施并补 evidence。",
    ),
)
```

验收（对齐计划表）：无服务时 UI 没有任何假成功按钮（本设计即声明
UNAVAILABLE）；断网→只读浏览；冲突→冲突副本可恢复；撤权→本地快照
保留可复制；跨项目引用→pinned_revision 保证重开一致。
