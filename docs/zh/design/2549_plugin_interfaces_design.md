# 系统设计：插件接口（Plugin Interfaces）

| 项 | 内容 |
| --- | --- |
| 设计源 | [RFC #2549 插件接口](https://github.com/oceanbase/powercontext/pull/2549) |
| 关联设计 | [插件热加载与自定义接口设计](2579_plugin_reload_and_apis_design.md)（RFC #2579） |
| 文档状态 | 设计草案 |
| 日期 | 2026-11-11 |
| 技术栈 | Python 3.11+ / pydantic v2 / SQLAlchemy 2.x / FastAPI；契约真源 `openapi/powercontext.yaml` |

本文是 RFC #2549 的实现设计：RFC 规定插件作者、调用方和部署者看到的契约，本文规定 core 如何实现这些契约，包括模块
划分、执行流程、进程模型、持久化结构、默认值和任务拆分。两者冲突时以 RFC 为准，并修订本文。

---

## 0. 不可违反的约束

| # | 约束 | 实现上的保证 |
| --- | --- | --- |
| C1 | Server 进程不导入插件代码 | 插件只在检查子进程、宿主进程和 Worker 中导入；Server 进程只持有检查子进程返回的可序列化声明 |
| C2 | 插件对核心数据的写入只经过写入器和 `collect` 返回值 | 插件代码拿不到 core 的数据库连接对象；写入器只暴露 RFC 中的写操作 |
| C3 | 游标、checkpoint、调用确认与核心数据在同一事务中提交 | 写事务块结束时由执行器在同一连接上完成游标 CAS、`invocation.complete` 和提交 |
| C4 | 用户修改恰好完成一项约定的写操作 | 写入器在用户修改模式下记录写操作，块结束时核对，不符则回滚并返回 502 |
| C5 | 插件自有存储不进入 core 的事务和 schema | core 只分配专属存储的位置，不创建、不迁移其中的表 |

---

## 1. 现状与差距

`feat/plugin-interfaces-v3` 分支已经实现 Artifact 插件的主体，下表列出与 RFC 的差距：

| 能力 | 现状 | 本文对应章节 |
| --- | --- | --- |
| `ArtifactPlugin` 声明、检查子进程、目录加载 | 已实现（`plugins.py`、`builtin/plugins/manifest.py`、`server/plugins.py`） | §3 |
| Worker 执行器、写入器、写事务块、游标与确认 | 已实现（`builtin/plugins/executor.py`、`builtin/plugins/writer.py`） | §4、§5 |
| 插件 family 的读取、检索、tags、通用合并接口 | 已实现（`builtin/plugins/families.py`、`builtin/plugins/search.py`、`server/app.py`） | §11 |
| 用户修改经过插件（五个方法） | 未实现；当前由 core 直接执行 `replace_plugin_artifact` 和合并引擎 | §6、§7 |
| 插件宿主进程 | 未实现 | §6 |
| `ctx.storage`、`ctx.models` | 未实现 | §9 |
| 代码指纹、事务外读取校验、`ArtifactChange`、内容不变时的写入规则 | 未实现 | §3.3、§5 |
| Source 插件（定义、`identify_target`、`collect`、采集目标） | 未实现 | §8 |
| 端侧模式 `source-agent` | 未实现 | §8.5 |
| 移除旧 Artifact 插件框架与旧 Source 插件框架 | 未实现 | §10 |

---

## 2. 模块与文件

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `src/powercontext/plugins.py` | 修改 | 公共契约：`ArtifactPlugin`、`SourcePlugin`、`Inputs`、`Interval`、`Cron`、`PluginContext`、`PluginStorage`、`PluginModels`、读写接口、`ArtifactChange`、用户修改请求模型、`InvalidContent`、Source 插件的请求与结果模型 |
| `src/powercontext/builtin/plugins/manifest.py` | 修改 | 导入 `plugin.py`、校验声明、导出可序列化声明；新增代码指纹计算；支持 `SourcePlugin` |
| `src/powercontext/builtin/plugins/executor.py` | 修改 | Worker 中的 `generate` 执行器；新增 `collect` 执行器 |
| `src/powercontext/builtin/plugins/writer.py` | 修改 | 写入器、写事务块、读取视图；新增用户修改模式、事务外读取的 revision 记录、`ArtifactChange` |
| `src/powercontext/builtin/plugins/host.py` | 新增 | 宿主进程入口：重建数据库连接与权限服务、导入插件、调用工厂、鉴权与版本预检、执行插件方法与写入器 |
| `src/powercontext/builtin/plugins/host_protocol.py` | 新增 | `PluginHostSpec` 与 Server 和宿主进程之间经 `Pipe` 传递的消息模型 |
| `src/powercontext/server/plugin_hosts.py` | 新增 | Server 侧的宿主进程管理：插件加载后在后台启动、复用、转发请求、超时终止、退避重启、停止 |
| `src/powercontext/builtin/plugins/storage.py` | 新增 | `PluginStorage` 实现：SQLite 文件路径与 OceanBase / seekdb 库的分配 |
| `src/powercontext/builtin/plugins/models.py` | 新增 | `PluginModels` 实现：封装 Server 的推理配置 |
| `src/powercontext/builtin/plugins/sources.py` | 新增 | Source 插件的定义注册、写入路径、采集目标仓库 |
| `src/powercontext/builtin/plugins/families.py` | 修改 | 插件 family 注册；移除插件时注销 |
| `src/powercontext/server/plugins.py` | 修改 | `PluginManager`：检查、注册、已激活插件记录、binding 注册、用户修改与 Source 写入的路由 |
| `src/powercontext/server/app.py` | 修改 | 通用新建接口的插件分支；PUT、合并、遗忘、恢复对插件 family 转交宿主进程；`/v1/plugins` 运维接口 |
| `src/powercontext/cli/source_agent.py` | 新增 | `powercontext source-agent` 命令 |
| `openapi/powercontext.yaml` | 修改 | 新建接口的插件分支、`/v1/plugins` 接口；修改后执行 `make api-generate` 与 `make contract-test` |
| `src/powercontext/builtin/persistence/migrations/` | 新增迁移 | 新增 `pc_source_collect_targets`，移除旧插件框架的表与列 |

---

## 3. 声明、检查与加载

### 3.1 检查子进程

`check_plugin_directory` 用 `python -m powercontext.builtin.plugins.manifest <目录>` 启动检查子进程，时限 60 秒。子进程：

1. 校验目录名匹配 `^[a-z][a-z0-9-]{0,63}$`，导入 `plugin.py`（模块名 `powercontext_plugin_<名字>`，插件目录加入 `sys.path`）；
2. 读取模块级变量 `plugin`，确认是 `ArtifactPlugin` 或 `SourcePlugin`，`create` 是 async 函数；
3. 用 Pydantic 校验声明，导出可序列化部分：family 或 Source 类型、各 Source 定义输入的 JSON Schema、`target_input` 的
   JSON Schema、`search_fields`、触发配置、实现了哪些用户修改方法；
4. 计算代码指纹（§3.3）；
5. 把结果以一行 JSON 输出到 stdout；失败时只输出已知的错误分类，不输出异常消息。

Server 收到结果后检查冲突：family 与内置 family 或其他插件重复、Source 类型与内置或其他插件重复，都判为检查失败。

各插件的检查并发执行，并发上限默认为 4。检查子进程只导入插件和 `powercontext.plugins`，不导入 core 的执行模块；在 2 核
开发机上，启动检查子进程并导入 `powercontext.builtin.plugins.manifest` 约 0.6 秒、33 MB，不含插件自身依赖的导入时间。

### 3.2 已激活插件记录

```python
@dataclass
class ActivePlugin:
    name: str
    directory: Path
    kind: Literal["artifact", "source"]
    manifest: PluginManifest | None        # 检查通过时为可序列化声明
    fingerprint: str | None                # 代码指纹
    declaration_digest: str | None         # 可序列化声明的 SHA-256
    error: str | None                      # 检查失败的分类
    host: PluginHostHandle | None          # 宿主进程句柄，见 §6
```

`PluginManager.plugins: dict[str, ActivePlugin]` 是每个 Server 进程的唯一记录。路由用户请求、启动宿主进程、注册 binding
都只读它；它不持久化，Server 启动时按插件目录重建。

### 3.3 代码指纹

```python
def code_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(directory).parts):
        digest.update(str(path.relative_to(directory)).encode()); digest.update(b"\0")
        digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()
```

- 检查子进程计算一次，记录在 `ActivePlugin.fingerprint` 和 `PluginWorkerSpec` 中。
- Worker 导入插件前、宿主进程启动时重新计算，不一致则失败，错误分类为 `plugin_changed`，提示重启 Server。
- 只按文件内容计算，不使用大小和修改时间。旧 Artifact 插件框架（`artifact_plugin_validation.code_fingerprint`）采用同样的
  算法。

### 3.4 注册

检查通过后，`PluginManager._register` 依次：

| 插件类型 | 注册内容 |
| --- | --- |
| Artifact 插件 | `register_plugin_family(family)`；在合并注册表中登记 `plugin_merge_service(family, ...)`，处理方式改为转交宿主进程（§7）；`ensure_plugin_search`；配置了 `inputs` 时 `register_source_binding`；本实例派发 Worker 时向 Supervisor 注册 binding |
| Source 插件 | 把各定义注册到 Source 定义清单（§8.1）；配置了 `collect` 时注册采集 binding |

binding 名为 `plugin:<name>`。`on_sources` 映射为 2 秒的 `automatic_processing_interval`；`Interval` 与 `on_sources` 同时
配置时取较小值；`Cron` 直接映射为 binding 的 cron。没有 `inputs` 的插件由 `_tick` 任务按 `Interval` 为 `scopes` 中的每个
Scope 写入调用意图。

`api` 角色的实例只注册 family 和 Source 类型，不注册 binding（`dispatch=False`）；`background` 角色的实例注册 binding，
但不接收请求，也不启动宿主进程。

---

## 4. Worker 执行器

### 4.1 generate

```text
Worker 收到 (plugin:<name>, scope_id, request_generation)
  -> 短事务：ScopeInvocation.start；读取 pc_source_cursors 中 (scope_id, plugin:<name>) 的游标
     -> 读取 journal 高水位，确定窗口 (after, through]，through ≤ after + max_sources
     -> list_window_for_generation 读取窗口，按 inputs.source_types 过滤，按每条 Source 的类型计算对应的 Projection（inputs.projections 中的类型专用值，否则为 inputs.projection）
  -> 窗口为空且配置了 inputs：短事务完成游标 CAS 与 acknowledge，不加载插件
  -> 核对代码指纹，导入 plugin.py，核对声明，调用 create，创建写入器，调用 generate（受 timeout_seconds 约束）
     -> 事务外读取：独立只读短事务，记录读到的 revision（§5.3）
     -> 写事务块开始：开启事务；guard（租约与调用意图）
        块内写操作在该事务中执行
        块结束：游标 CAS 到 through；acknowledge；提交
  -> generate 返回且未开启写事务块：短事务完成游标 CAS 与 acknowledge
  -> 写事务块提交后 generate 抛出异常：记录为提交后错误，不重试该窗口
```

- 窗口内类型不匹配的 Source 被过滤，游标仍推进过整个窗口；匹配类型的 Source 缺少所需 Projection 时调用失败。
- 游标 CAS 以读取时的 `generation` 为期望值，保证领导权变更或重复派发时同一窗口只提交一次。
- 待处理工作的发现：Server 进程中的 Source 写入把关注该 Scope 的插件 binding 标记为待处理；启动时
  `SourceProcessingPendingProvider` 按 journal 高水位与游标的差值补齐其他进程写入的 Source。
- 数据库就绪检查会校验每个待处理调用意图的 binding 都已注册；`plugin:` 前缀的 binding 由插件目录决定，就绪检查对其放行，
  插件移除后遗留的调用意图不再被处理。
- Worker 打开数据库时不创建表（`tables=()`），schema 由 Server 准备。

### 4.2 collect

```text
Worker 收到 (plugin:<name>, scope_id, request_generation)
  -> 短事务：ScopeInvocation.start；读取该 Scope、该插件已启用的采集目标，按目标键轮转取至多 max_targets 个，
     has_more 的目标优先；读取各目标的 checkpoint（pc_connector_checkpoints，键为目标的 binding ID）
  -> 核对代码指纹，导入插件，调用 create，调用 collect（受 timeout_seconds 约束）
  -> 校验返回值：条目总数 ≤ max_items；目标键都在请求中；每条按定义的 input_class 校验，执行 resolve 与 Projection
  -> 一个事务：保存全部 Source（沿用身份去重）；以旧 checkpoint 为期望值写入各目标的新 checkpoint；更新最近采集时间；
     acknowledge；提交
  -> 任一步失败：不提交任何内容；记录各目标的最近失败分类（单独的短事务）；进入 Supervisor 退避
```

添加或恢复采集目标时，`ArtifactProcessingIntentRepository.request` 写入一次调用意图并唤醒 binding。

---

## 5. 写入器

### 5.1 写操作与复用的 core 实现

| 写操作 | 复用的实现 |
| --- | --- |
| `put` | 共享合并引擎的修改路径（`ArtifactMergeService.prepare_change` / `commit`），新建时走 `ArtifactRepository` 的创建 |
| `revert` | 共享恢复流程 `inspect_restore(operation="restore", revision=...)` |
| `set_lifecycle` | `deprecated` / `active` 走共享的遗忘与恢复流程；`retired` 与指定替代者使用 `ArtifactGovernanceRepository.transition` |
| `merge` | 共享合并引擎（RFC #1859） |
| `preview_undo_merge` / `undo_merge` | 共享合并引擎的撤销预览与恢复 |
| `bind_handle` | Artifact handle 绑定实现 |
| `set_tags` | tags 替换实现（`builtin/persistence/tags.py`） |

`revert` 和 `set_lifecycle` 把共享流程返回的 `ArtifactMergeMutationResult` 转为 `ArtifactChange`：`artifact` 为目标的新快照，
`restored` 与 `retired` 取自结果中的 `restored` 和 `retired`（不含目标本身）。

### 5.2 内容不变时的写入

- 后台调用（`generate`）中，`put` 的内容与当前 revision 相同则不写入，返回当前快照，传入的 `source_refs` 和 `artifact_refs`
  被忽略。这与 Atomic Memory 的 reconcile 一致：`noop` 决策只更新内存中的证据，写出时只写 `changed` 的条目
  （`builtin/artifacts/atomic_memory/reconciliation.py`）。
- 用户修改模式中，`put` 总是追加 revision，与内置 family 的替换（`records.py` 中的 `artifact_replace`）一致。

### 5.3 事务外读取的 revision 校验

写入器维护 `observed: dict[str, int]`：事务外的 `get`、`list`、`search`、`by_source` 每返回一个当前快照，就记录其
`artifact_id → revision`；同一 ID 读到多次时保留最早的一次。写事务块中的 `put` 没有给出 `expected_revision` 时，若该 ID 在
`observed` 中，就以记录的 revision 作为期望值；不在其中则不校验。读取指定历史 revision（`get(..., revision=n)`）不记录。

这对应 Atomic Memory 在提交前核对所读记忆是否变化的做法（`AtomicMemoryConflictError("A recalled memory changed during
window preparation")`）。

### 5.4 写入范围与上限

| 规则 | 实现 |
| --- | --- |
| 只写本 Scope、本 family | 写操作的 Scope 与 family 固定在写入器上，参数中没有这两项 |
| `source_refs` 范围 | `generate` 中限于 `window_sources`；用户修改中限于目标 Artifact 当前 lineage 中的 Source |
| `artifact_refs` 范围 | 本 Scope 中任意 family 实际存在的 revision |
| 单块操作数 | 1000（`MAX_WRITES_PER_TRANSACTION`） |
| 单份内容大小 | 256 KiB（UTF-8 JSON 序列化后） |
| 单块总字节数 | 8 MiB |
| 写事务块时限 | `transaction_timeout_seconds`，默认 30 秒 |
| 每次调用的写事务块数 | 1；第二次开启抛出异常 |

超出上限即抛出异常并回滚，不截断。

### 5.5 用户修改模式

宿主进程为用户修改创建写入器时传入 `mode=UserChange(operation, target)`：

- 写事务块内只允许 §7.2 表中约定的那一项写操作，以及对同一 Artifact 的 `set_tags`；其他写操作立即抛出 `PluginWriteError`。
- 块结束时检查约定的写操作是否恰好完成一次；未完成则回滚，宿主进程返回 `contract_violation`，Server 映射为 502。
- 写操作以调用者身份授权：写入器的 `_Security` 使用请求携带的调用者，而不是 Worker 的部署身份；权限服务是宿主进程中
  重建的那一份（§6.1）。
- 新建、编辑、恢复、撤销合并时，写入器在同一事务中写入 `lineage_only` Content Source（§7.3）。

---

## 6. 插件宿主进程

### 6.1 进程模型

宿主进程是 **core 运行时加插件**：它与 Worker 一样，从配置自建数据库连接、权限服务和所需的 core 服务，在此之上加载插件。
Server 进程只负责认证调用者和转发请求，鉴权、版本预检、写入都在宿主进程中完成。

- 每个 Server 进程为每个需要同步调用的插件维护至多一个宿主进程。需要宿主进程的插件：实现了任一用户修改方法的 Artifact
  插件、提供 Source 定义的 Source 插件、提供 `identify_target` 的 Source 插件。
- 插件加载成功后，Server 在后台启动其宿主进程，不等到第一次同步调用。宿主进程导入 core 需要数秒（Worker 实测约 5.5 秒、
  150 MB），预先启动避免第一个请求承担这段时间；宿主进程尚未就绪时到达的请求等待就绪，等待时间计入请求时限。
- 启动方式与 Worker 相同：`multiprocessing.get_context("spawn")` 启动子进程，复用 `SpawnArtifactProcessingWorkerLauncher`
  中的启动、取消和回收逻辑；子进程以全新解释器启动，不继承 Server 进程已导入的模块。
- 启动参数 `PluginHostSpec` 可以 pickle，内容与 `PluginWorkerSpec` 对应：数据库配置、推理配置、权限配置
  （`WorkerSecuritySpec`）、插件目录、可序列化声明、代码指纹。
- 宿主进程启动时依次：核对代码指纹；打开数据库连接（`open_plugin_database`，不建表）；用 `open_worker_security` 重建权限
  服务；重新注册 Server 进程中只存在于内存的注册项（插件 family、权限 profile、合并注册表中的本插件 family，做法同 Worker
  的 `_restore_access_families`）；导入 `plugin.py` 并核对声明；调用一次 `create(ctx)`（`ctx.scope_id` 为 `None`）；打开
  专属存储；发送 `ready`。
- 宿主进程是单个 asyncio 事件循环，可以并发处理多个请求，并发上限默认 8；超过上限的请求在 Server 侧排队，排队时间计入
  该请求的时限。

### 6.2 通信

Server 与宿主进程之间是一条双向 `multiprocessing` `Pipe`，消息是可 pickle 的对象，`Pipe` 负责分帧。插件代码写到
stdout / stderr 的内容不会进入这条通道，bytes 可以直接放在消息里，不需要另外的文件交换。消息模型定义在
`host_protocol.py`：

```python
class HostRequest(BaseModel):
    id: str
    method: Literal["resolve", "identify_target", "create_artifact", "update_artifact",
                    "merge_artifacts", "forget_artifact", "restore_artifact"]
    scope_id: str
    caller: Principal | None               # Server 认证得到的调用者；Source 写入与用户修改时必填
    preconditions: dict[str, JsonValue]    # 调用方提交的 If-Match、revision、state_version
    deadline_seconds: float
    payload: dict[str, JsonValue]          # 对应请求模型的 JSON

class HostResponse(BaseModel):
    id: str
    outcome: Literal["ok", "invalid_content", "not_implemented", "forbidden", "precondition_failed",
                     "conflict", "contract_violation", "error"]
    result: dict[str, JsonValue] | None = None   # 成功时的快照、Source 引用或 Artifact ID
    message: str | None = None                   # 仅 invalid_content 时携带插件给出的消息
    field: str | None = None
    error_type: str | None = None                # error 时只携带异常类型名
```

Server 端的 `PluginHostHandle` 在一个后台任务中读取 `Pipe`，按 `id` 把响应交给等待的请求。

### 6.3 超时、故障与重启

| 情况 | 处理 |
| --- | --- |
| 单个请求超过时限 | Server 终止整个宿主进程（`terminate`，5 秒后 `kill`），进行中的全部请求返回 503 |
| 进程退出、`Pipe` 关闭或收到无法识别的消息 | 同上 |
| 重启 | 退避后在后台重新启动；退避从 1 秒开始，每次翻倍，上限 60 秒；连续运行 5 分钟无故障后清零 |
| 退避期间 | 该插件的同步调用直接返回 503；后台调用不受影响 |
| Server 停止 | 停止接受同步调用，等待进行中的请求至多 10 秒，然后终止宿主进程 |

同步调用的时限：Source 写入与 `identify_target` 默认 10 秒（`POWERCONTEXT_SERVER_PLUGIN_SYNC_TIMEOUT_SECONDS`），用户修改
使用声明中的 `change_timeout_seconds`。时限从 Server 收到请求开始计算，包括等待宿主进程就绪和排队的时间。

### 6.4 宿主进程中的写入

宿主进程中的写入器与 Worker 使用同一实现，区别是：

- 没有游标和调用确认；写事务块结束时只提交本次写入。
- 授权使用请求携带的调用者，由宿主进程中重建的权限服务判断，与内置 family 的授权规则相同。
- 写事务块之外的读取同样记录 `observed`（§5.3）。

---

## 7. 用户修改调用链

### 7.1 流程

```text
HTTP 请求（新建 / PUT / 合并 / 遗忘 / 恢复）
  -> Server：认证；按 family 找到插件；插件未实现对应方法 → 422（声明中记录了实现的方法，不必进入宿主进程）
  -> Server：把 HostRequest（调用者、版本前置条件、请求内容）交给宿主进程，等待 HostResponse
  -> 宿主进程：鉴权（与内置 family 相同）→ forbidden
  -> 宿主进程：版本预检（If-Match → precondition_failed；revision / state_version → conflict），不合格时不调用插件
  -> 宿主进程：新建与合并时分配 artifact_id / into
  -> 宿主进程：构造请求模型，调用插件方法；写入器在写事务块中完成约定的写操作，并写入 lineage_only Content Source；
     提交时再次校验版本，被其他写入改变则回滚并返回 conflict
  -> Server：按 HostResponse 映射 HTTP 响应（§7.4）
```

### 7.2 约定的写操作

| 方法 | 约定 |
| --- | --- |
| `create_artifact` | 恰好一次 `put(request.artifact_id, ...)`，目标此前不存在 |
| `update_artifact` | 恰好一次 `put(request.artifact_id, ..., expected_revision=request.expected_revision)` |
| `merge_artifacts` | 恰好一次 `merge(输入 ID, into=request.into, ...)`，输入与请求一致 |
| `forget_artifact` | 恰好一次 `set_lifecycle(request.artifact_id, "deprecated")` |
| `restore_artifact` | `restore`：恰好一次 `revert` 或 `set_lifecycle(..., "active")`；`undo_merge`：恰好一次 `undo_merge(request.artifact_id)` |

### 7.3 溯源

| 操作 | Content Source 的 `operation` | 目标 revision |
| --- | --- | --- |
| 新建 | `artifact_create` | 1 |
| 编辑 | `artifact_replace` | 新 revision |
| 恢复（追加 revision 时） | `artifact_restore` | 新 revision |
| 撤销合并 | `artifact_undo_merge` | 恢复的各输入的新 revision |

Content Source 的内容为调用方提交的内容（规范化前），`role="lineage_only"`，与 `records.py` 中内置 family 的写法相同，不进入
任何插件的输入窗口。

### 7.4 错误映射

| `HostResponse.outcome` / 情况 | HTTP |
| --- | --- |
| `ok` | 与内置 family 相同（新建 201，其他 200） |
| `forbidden` | 403 |
| `precondition_failed` | 412 |
| `invalid_content` | 422，带插件的消息和字段 |
| `not_implemented` | 422 |
| `conflict` | 409 |
| `contract_violation` | 502 |
| `error` | 500，只记录异常类型 |
| 超时、宿主进程不可用、退避中 | 503 |

---

## 8. Source 插件

### 8.1 定义注册与写入

- 启动检查得到各定义的声明式清单（Source 类型、输入 JSON Schema、Projection 键），注册到 Source 定义清单
  （`pc_source_definition_manifests`）。注册沿用现有的清单登记逻辑：已登记的清单与插件给出的完全相同时直接复用，内容不同才判为冲突，检查失败。
- `POST /v1/scopes/{scope_id}/sources` 与 MCP `pc_source_add` 的 write 用法遇到插件 Source 类型时，Server 把输入交给宿主进程
  执行 `input_class` 校验、`resolve` 和 Projection（`HostRequest.method="resolve"`），拿回 Source 与 Projection 后在 Server
  进程中保存，沿用身份去重。

### 8.2 采集目标表

```text
pc_source_collect_targets
  scope_id            VARCHAR(256)  PK
  plugin_name         VARCHAR(64)   PK
  target_key          VARCHAR(512)  PK
  binding_id          VARCHAR(256)  NOT NULL   -- 由插件名和目标键派生，超长时取 SHA-256
  normalized_input    JSON          NOT NULL
  enabled             BOOLEAN       NOT NULL
  created_at          TIMESTAMP     NOT NULL
  updated_at          TIMESTAMP     NOT NULL
  last_collected_at   TIMESTAMP     NULL
  last_failure        VARCHAR(64)   NULL       -- 最近失败分类
```

### 8.3 目标管理

| 操作 | 实现 |
| --- | --- |
| 添加 | 用 `target_input` 的 JSON Schema 在 Server 进程中校验；宿主进程执行 `identify_target`；同键且规范化输入相同返回已有目标，同键不同输入返回 409；新目标写入后请求一次采集 |
| 查看 | 读取目标表与 checkpoint |
| 停用 / 恢复 | 更新 `enabled`；恢复时请求一次采集 |

### 8.4 binding ID

`binding_id = f"plugin:{plugin_name}:{target_key}"`，超过 256 字符时为 `f"plugin:{plugin_name}:sha256:{digest}"`。端侧模式在
前面加上端侧标识（§8.5）。

### 8.5 端侧模式

`powercontext source-agent --server URL` 启动一个只包含 Source 插件采集的进程：

| 组成 | 实现 |
| --- | --- |
| 本地存储 | 端侧数据目录下的 SQLite，只建采集目标表与 Supervisor 的调度表 |
| 调度 | 本地 Supervisor，只注册 Source 插件的采集 binding |
| 目标管理 | 本地的 `pc_source_add`、`pc_source_status`、`pc_source_target_set_enabled` |
| 定义登记 | 启动时对每个定义调用 `POST /v1/source-definitions/register`；凭据没有 `server.admin` 时跳过，要求事先登记 |
| 提交 | 复用 `client/ingestion.py` 的 `RemoteConnectorWorker` 与其 Source sink、checkpoint store |
| 宿主进程 | 不启动；`resolve` 与 Projection 在采集 Worker 中执行 |

每次 `collect`：

1. `POST /v1/connector-checkpoints/get` 读取各目标的 checkpoint；
2. 调用插件的 `collect`；
3. 在 Worker 中执行 `resolve` 与 Projection，逐条 `POST /v1/source-observations` 提交；
4. 全部成功后，以旧值为期望值 `POST /v1/connector-checkpoints/commit`；
5. 任一步失败则本次调用失败，进入退避；已提交的 Source 保留，下次重采由中心按身份去重。

端侧的 binding ID 为 `f"agent:{agent_id}:plugin:{plugin_name}:{target_key}"`，同样按长度取摘要。

---

## 9. 专属存储与模型

### 9.1 专属存储

| 后端 | 位置 | 分配 |
| --- | --- | --- |
| SQLite | `<数据目录>/plugins/<插件名>.sqlite3` | 首次调用 `engine()` 时创建文件，开启 WAL |
| OceanBase / seekdb | 库 `powercontext_plugin_<插件名>`（`-` 换成 `_`） | 首次调用 `engine()` 时执行 `CREATE DATABASE IF NOT EXISTS`；没有权限时报错，提示部署者事先建库 |

- `engine()` 返回 `AsyncEngine`，URL 由 Server 的数据库配置替换库名或文件路径得到，使用同一登录。
- Worker 中每次调用创建 engine，调用结束时释放；宿主进程中在进程存续期间复用。
- 移除插件不删除这块存储。

### 9.2 模型

`PluginModels` 由宿主进程或 Worker 根据 Server 传入的推理配置构造，复用 core 的生成模型和 embedding 模型客户端：

- `embed(texts)` 调用 Server 配置的 embedding 模型，`embedding_profile` 给出 profile ID 与维度；
- `generate(messages, max_tokens)` 调用 Server 配置的生成模型；
- 调用计入现有的模型用量统计，`operation` 标为 `plugin:<name>`；
- 推理配置通过 Worker spec 和宿主进程启动参数传递，插件代码拿不到其中的地址和凭据。

---

## 10. 持久化与迁移

一次新的数据库迁移完成以下变更：

| 变更 | 对象 |
| --- | --- |
| 新增 | `pc_source_collect_targets`（§8.2） |
| 移除列 | `pc_artifact_processing_binding_states` 中的 `plugin_id`、`plugin_family`、`binding_generation`、`desired_state`、`phase`、`last_error`、`plugin_spec` |
| 移除表 | `pc_artifact_family_schemas`；`pc_source_write_plugins`；自定义 family 声明式索引的搜索状态与向量索引表（确认内置 family 不依赖后） |
| 删除数据 | 旧 Artifact 插件写入的 Artifact、head、lineage（按旧插件 family 范围删除） |

不受影响：`pc_sources`、Projection、journal、`pc_source_definition_manifests`、`pc_artifacts` 及其 heads、lineage、合并记录、
handle、tags；`pc_source_cursors`；`pc_connector_checkpoints`；Supervisor 的调用意图、pending、租约表。

迁移说明列出新增、删除的表、列和数据范围。同时移除的代码：`/v1/admin/artifact-plugins` 及其实现、`source_management`
包（管理服务、Runtime、poll / reply 通道、`source load`）、旧 Worker 插件执行器、`ArtifactSearchDeclaration` 等声明式索引。

---

## 11. 公开读取与检索

- 插件 family 的 Artifact 以 `PluginArtifactContent` 保存：`content` 原样保存，`searchable_text` 由 `search_text(content,
  search_fields)` 提取后经现有词法分析写入 `pc_artifact_heads.searchable_text`，随 revision 保存。
- SQLite 上与 Experience、Skill 共用 `pc_artifact_fts`；`ensure_plugin_search` 保证辅助表存在，重建时回填所有带检索文本的
  head，就绪校验只比对内置 family 的行。
- OceanBase 上的检索方式与规模验证在实现 PR 中给出。
- 移除插件后，其 family 不再注册，通用接口返回 404；数据保留。

---

## 12. 可观测性

| 字段 | 含义 |
| --- | --- |
| `plugin_name` | 插件名 |
| `plugin_method` | `collect`、`generate`、`resolve`、`identify_target`、`create_artifact`、`update_artifact`、`merge_artifacts`、`forget_artifact`、`restore_artifact` |
| `plugin_process` | `worker` 或 `host` |
| `trigger` | `schedule`、`sources`、`target_added`；同步调用为 `request` |
| `operation_count` | 本次提交的写操作数（Artifact）或 Source 条数（collect） |
| `target_count` | 本次 collect 携带的目标数 |
| `transaction_ms` | 写事务块从开始到提交或回滚的耗时，毫秒 |

- 宿主进程的启动、终止和重启各记录一条事件，带 `plugin_name` 和终止原因（`timeout`、`crash`、`server_stop`）。
- 指标标签使用 `plugin_name`、`plugin_method`、`plugin_process`，不使用 Scope ID 或目标键。
- 日志经 `log_safely` 输出，不包含 Source 内容、Projection、Artifact 内容、目标输入和环境变量值；插件异常只记录类型。
- 新字段登记到允许列表，并在中英文可观测性文档中说明。

---

## 13. 默认值汇总

| 项 | 默认值 | 配置 |
| --- | --- | --- |
| 检查子进程时限 | 60 秒 | 固定 |
| 检查并发上限 | 4 | 固定 |
| 写事务块时限 | 30 秒 | 声明 `transaction_timeout_seconds` |
| Worker 总时限 | 300 秒（Artifact）、120 秒（collect） | 声明 |
| 用户修改时限 | 30 秒 | 声明 `change_timeout_seconds` |
| Source 写入与 `identify_target` 时限 | 10 秒 | `POWERCONTEXT_SERVER_PLUGIN_SYNC_TIMEOUT_SECONDS` |
| 宿主进程并发上限 | 8 | `POWERCONTEXT_SERVER_PLUGIN_HOST_CONCURRENCY` |
| 宿主进程就绪超时 | 60 秒 | 固定 |
| 宿主进程重启退避 | 1 秒起，翻倍，上限 60 秒 | 固定 |
| 写事务块操作数 / 单份内容 / 总字节 | 1000 / 256 KiB / 8 MiB | 固定 |
| `on_sources` 接纳间隔 | 2 秒 | 固定 |

---

## 14. 任务拆分

| # | 任务 | 依赖 |
| --- | --- | --- |
| T1 | 写入器：内容不变规则、事务外读取校验、`ArtifactChange`、用户修改模式 | — |
| T2 | 代码指纹：检查子进程计算，Worker 导入前核对 | — |
| T3 | 宿主进程：`host.py`、`host_protocol.py`、`server/plugin_hosts.py`，复用 Worker 的 spawn 启动与回收，重建权限服务与内存注册项，含超时、退避、停止 | T2 |
| T4 | 用户修改经过插件：Server 路由、版本预检、错误映射、`lineage_only` Source；替换当前的 core 直接执行 | T1、T3 |
| T5 | 新建接口的插件分支：OpenAPI、生成代码、Server 实现 | T4 |
| T6 | `ctx.storage`、`ctx.models` | T3 |
| T7 | Source 插件：声明、定义注册、宿主进程中的写入路径 | T3 |
| T8 | 采集：目标表迁移、目标管理接口、collect 执行器 | T7 |
| T9 | 端侧模式 `source-agent` | T8 |
| T10 | 移除旧插件框架与迁移 | T4、T8 |
| T11 | 可观测性字段登记与文档 | T3、T8 |

---

## 15. 待明确事项

| 事项 | 说明 |
| --- | --- |
| OceanBase 上插件 family 的检索 | 实现方式与规模验证在 T5 前确定 |
| 宿主进程的默认参数 | 并发上限、同步时限、退避参数需要用真实插件压测后确认 |
| 专属存储的建库权限 | 没有建库权限时是否允许退回到 core 库中按插件名前缀建表 |
| 检查并发上限 | 4 是否合适，需要在插件较多的部署中测量 |

---

## 16. RFC 之外的设计决定

RFC 只规定契约。下表列出本文为实现契约所做、RFC 没有规定的决定，评审时逐项确认。"建议修改"表示已有更好的方案，
确认后改写对应章节。

| # | 决定 | 理由 | 状态 |
| --- | --- | --- | --- |
| D1 | 宿主进程用 `multiprocessing` 的 spawn 启动，Server 与宿主进程之间通过双向 `Pipe` 传递可 pickle 的消息（§6.1、§6.2） | 与现有 Worker 启动器（`SpawnArtifactProcessingWorkerLauncher`）相同，启动、取消、回收可以复用；插件写 stdout 不会破坏通信；bytes 可以直接传递；跨平台。stdin / stdout 按行传 JSON 需要自己分帧，且会被插件的输出破坏 | 已采用 |
| D2 | 宿主进程是"core 运行时 + 插件"：像 Worker 一样从配置自建数据库连接和权限服务（`open_worker_security`），重新注册内存中的注册项；Server 只认证调用者并转发请求（§6.1） | 数据库连接不能跨进程传递，Worker 已经采用这种方式；鉴权、Scope 管理和写入在同一进程中完成，不需要回调 Server。代价是每个宿主进程启动时导入整套 core，约 5.5 秒、150 MB | 已采用 |
| D3 | 用户修改的鉴权和版本预检在宿主进程中执行，不合格时不调用插件（§7.1） | 随 D2，Server 只做认证和转发 | 已采用 |
| D4 | 插件未实现某个用户修改方法时，由 Server 按声明直接返回 422（§7.1） | 不必进入宿主进程 | 已采用 |
| D5 | 各插件的检查并发执行，上限 4（§3.1） | 检查子进程约 0.6 秒；插件多时串行检查会拖慢启动 | 已采用，上限待测量 |
| D6 | 事务外读取记录同一 Artifact 最早读到的 revision，读取历史 revision 不记录（§5.3） | 最早一次读取是插件做决定的起点 | 已采用 |
| D7 | 中心已登记的 Source 定义清单与插件给出的完全相同时视为同一定义，内容不同才算冲突（§8.1） | 沿用现有的清单登记逻辑（`SourceDefinitionManifestRepository.register` 对相同清单返回已有记录，内容不同才报冲突）；多个端侧登记同一定义、或同一插件同时装在端侧和中心时都依赖这一行为 | 已采用 |
| D8 | 采集目标的 binding ID 格式，超长时取摘要；端侧加端侧标识前缀（§8.4、§8.5） | 确定性派生；两个端侧采集同一外部资源时不互相覆盖 checkpoint | 已采用 |
| D9 | 专属存储的 engine 在 Worker 中每次调用创建，在宿主进程中复用（§9.1） | 与工厂实例的生命周期一致 | 已采用 |
| D10 | 插件调用模型的用量统计记为 `plugin:<name>`（§9.2） | 区分插件与内置能力的模型用量 | 已采用 |
| D11 | 单个请求超时时终止整个宿主进程（§6.3） | asyncio 无法可靠中断一个阻塞的请求 | 已采用 |
| D12 | §13 中的同步调用时限、宿主进程并发上限和重启退避 | 初始值 | 待压测确认 |
| D13 | 插件加载成功后在后台预先启动宿主进程，而不是等第一次同步调用（§6.1） | 宿主进程启动需要数秒，不应由第一个请求承担；RFC #2549 参考级第 6 节相应写为插件加载后启动 | 已采用 |
