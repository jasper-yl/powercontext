# 系统设计：插件接口（Plugin Interfaces）

| 项 | 内容 |
| --- | --- |
| 设计源 | [RFC #2549 插件接口](https://github.com/oceanbase/powercontext/pull/2549) |
| 关联设计 | [插件热加载与自定义接口设计](2579_plugin_reload_and_apis_design.md)（RFC #2579）；Source 插件与采集见 [RFC #2617](https://github.com/oceanbase/powercontext/pull/2617) |
| 实现 | [oceanbase/powercontext#2616](https://github.com/oceanbase/powercontext/pull/2616) |
| 日期 | 2026-11-13 |
| 技术栈 | Python 3.11+ / pydantic v2 / SQLAlchemy 2.x / FastAPI；契约真源 `openapi/powercontext.yaml` |

本文是 RFC #2549 的实现设计：RFC 规定插件作者、调用方和部署者看到的契约，本文规定 core 如何实现这些契约，包括模块
划分、执行流程、进程模型、持久化结构和默认值。两者冲突时以 RFC 为准，并修订本文。第 2 节列出各部分在 #2616 中的实现
状态；未实现的部分描述目标设计。

---

## 0. 插件要实现的接口

插件分为两个模板：Artifact 插件（本文，RFC #2549 与 #2579）和 Source 插件（RFC #2617）。两者都是插件目录下的一个子目录，
`plugin.py` 把声明赋给模块级变量 `plugin`，目录名就是插件名。

### 0.1 Artifact 插件：声明

`plugin = ArtifactPlugin(...)` 的字段。"必需"一列中的"条件"表示在某种配置下才必需。

| 字段 | 必需 | 默认值 | 说明 | 来源 |
| --- | --- | --- | --- | --- |
| `family` | 是 | — | 插件拥有的 Artifact family，不能与内置 family 或其他插件重复 | #2549 |
| `setup` | 是 | — | async 工厂，返回插件对象；Worker 每次调用执行一次，宿主进程中执行一次后复用 | #2549 |
| `inputs` | 条件：消费 Source 时 | `None` | `Inputs(source_types, projection, projections, max_sources=50)` | #2549 |
| `schedule` | 否 | `None` | `Interval` 或 `Cron`；`Cron` 需要 `inputs`，且不能与 `on_sources` 同时使用；`Cron` 的时区默认为 Server 所在机器的时区 | #2549 |
| `on_sources` | 否 | `False` | 新 Source 到达后触发；需要 `inputs` | #2549 |
| `scopes` | 条件：有 `Interval` 但没有 `inputs` 时 | `None` | 服务的 Scope 名单 | #2549 |
| `definitions` | 否 | `()` | 插件自带的 Source 定义 | #2549 |
| `max_workers` | 否 | 1 | 跨 Scope 的并发上限 | #2549 |
| `host_processes` | 否 | 1 | 每个 Server 进程中本插件宿主进程数的建议上限，部署者可以覆盖 | #2549 |
| `timeout_seconds` | 否 | 300 | 一次后台生成的时限 | #2549 |
| `transaction_timeout_seconds` | 否 | 30 | 写事务块的时限 | #2549 |
| `change_timeout_seconds` | 否 | 30 | 一次用户修改、展示或检索的时限 | #2549 |
| `api` | 否 | `None` | `PluginApi(routes=...)`，插件自定义接口 | #2579 |
| `pages` | 否 | `None` | `PluginPages(directory, entry, title)`，Dashboard 页面 | #2579 |
| `upgrade` | 否 | `None` | 每次加载插件版本时调用的数据升级钩子（未实现） | #2579 |
| `upgrade_timeout_seconds` | 否 | 600 | `upgrade` 的时限（未实现） | #2579 |

声明在构造时校验：`inputs` 必须配合 `schedule` 或 `on_sources`，两者都不配置时插件没有后台生成，Artifact 只由用户修改和
插件接口写入；`transaction_timeout_seconds` 不超过 `timeout_seconds`；声明 `pages` 时必须声明 `api`。

### 0.2 Artifact 插件：插件对象的方法

`setup(ctx)` 返回的对象上的方法。宿主进程在 `setup` 之后检查必需的方法，缺少时插件为 `failed`。

| 方法 | 必需 | 何时调用 | 执行位置 |
| --- | --- | --- | --- |
| `render_artifact(request) -> Rendering` | 是 | `GET .../artifacts/{family}/{artifact_id}/rendering` | 宿主进程 |
| `generate(request, writer)` | 条件：配置了 `schedule` 或 `on_sources` | 定时，或新 Source 到达 | Worker |
| `search_artifacts(request) -> SearchResult` | 否；未实现时检索返回 422 | 通用检索接口；Prepared Context 中列出该 family | 宿主进程 |
| `create_artifact(request, writer)` | 否；未实现时返回 422 | 通用新建接口 | 宿主进程 |
| `update_artifact(request, writer)` | 否 | `PUT` 替换 | 宿主进程 |
| `merge_artifacts(request, writer)` | 否 | 通用合并接口 | 宿主进程 |
| `forget_artifact(request, writer)` | 否 | 通用遗忘接口 | 宿主进程 |
| `restore_artifact(request, writer)` | 否 | 恢复、恢复到旧 revision、撤销合并 | 宿主进程 |
| 路由的处理函数 `handler(request)` | 条件：声明了 `api` | `/v1/plugins/{name}/api{path}` | 宿主进程 |

列出、读取当前内容或指定 revision、revision 历史、治理状态和 tags 不调用插件，由 core 直接提供。

### 0.3 Source 定义

插件在 `definitions` 中提供 Source 类型时，每个定义要实现：

| 成员 | 必需 | 说明 |
| --- | --- | --- |
| `name`、`version` | 是 | Source 类型名与定义版本，不能与内置类型重复 |
| `input_class` | 是 | 写入内容的 Pydantic 模型 |
| `resolve(value)` | 是 | 纯函数：给出 Source 身份和规范内容 |
| Projection 的 `project`、`read` | 是 | 把 Source 转为 Artifact 插件消费的视图 |

### 0.4 `ctx` 提供的能力

| 能力 | Worker | 宿主进程 | 说明 |
| --- | --- | --- | --- |
| `ctx.name`、`ctx.scope_id`、`ctx.plugin_dir`、`ctx.logger` | 有 | 有（`scope_id` 为 `None`） | 基本信息 |
| `ctx.storage` | 有 | 有 | 专属存储，第 9 节 |
| `ctx.models` | 有 | 有 | Server 配置的模型，第 9 节 |
| `ctx.enter_async_context(...)` | 有 | 有 | 随调用或进程关闭的资源 |
| `ctx.caller`、`ctx.require`、`ctx.allowed` | — | 有 | 当前请求的调用者及权限检查 |
| `ctx.reads(scope_id)`、`ctx.writer(scope_id)` | — | 有 | 以调用者身份读写本 family |
| `ctx.sources.submit(...)` | — | 有 | 以调用者身份提交本插件的 Source |
| `ctx.scopes.create/update/list` | — | 有 | 管理本插件创建的 Scope |

### 0.5 Source 插件（RFC #2617）

Source 插件的契约由 RFC #2617 规定，尚未实现，这里只列出接口以便对照：

| 接口 | 必需 | 说明 |
| --- | --- | --- |
| `version` | 是 | 插件版本，按 PEP 440 比较；中心据此核对设备版本 |
| `min_compatible_version` | 否 | 中心仍接受的最低设备版本，默认等于 `version` |
| `definitions` | 是 | Source 定义，同 0.3 |
| `target` | 条件：采集时 | 目标参数模型与唯一性表达式 |
| `collect` | 条件：采集时 | 调度和上限；插件对象实现 `collect(request) -> CollectResult` |
| `setup` | 条件：采集时 | async 工厂 |
| `upgrade` | 否 | 只在中心加载时调用 |

---

## 1. 不可违反的约束

| # | 约束 | 实现上的保证 |
| --- | --- | --- |
| C1 | Server 进程不导入插件代码 | 插件只在检查子进程、宿主进程和 Worker 中导入；Server 进程只持有检查子进程返回的可序列化声明 |
| C2 | 插件对核心数据的写入只经过写入器和 `ctx.sources.submit` | 插件代码拿不到 core 的数据库连接对象；写入器只暴露 RFC 中的写操作 |
| C3 | 游标、调用确认与核心数据在同一事务中提交 | 写事务块结束时由执行器在同一连接上完成游标 CAS、`invocation.complete` 和提交 |
| C4 | 用户修改恰好完成一项约定的写操作 | 写入器在用户修改模式下记录写操作，块结束时核对，不符则回滚并返回 502 |
| C5 | 插件自有存储不进入 core 的事务和 schema | core 只分配专属存储的位置，不创建、不迁移其中的表 |

---

## 2. 实现状态

| 部分 | 状态 | 章节 |
| --- | --- | --- |
| 声明、检查子进程、代码指纹、注册 | #2616 已实现 | §4 |
| Worker 执行器、写入器、写事务块 | #2616 已实现 | §5、§6 |
| 宿主进程、用户修改、展示、检索、Prepared Context | #2616 已实现 | §7、§8 |
| 宿主进程组（按负载增减、总上限）、超时只取消单个请求、心跳、Prepared Context 省略不可用插件、`Cron` 默认机器时区 | #2616 已实现 | §7.1、§7.3、§8.5 |
| 插件自带 Source 类型 | #2616 已实现 | §8.6 |
| 专属存储 | #2616 已实现 SQLite；OceanBase / seekdb 未实现 | §9.1 |
| 模型 | #2616 已实现 | §9.2 |
| 移除 #2246 Artifact 插件框架、迁移 `v1_5_0_005` | #2616 已实现 | §10 |
| 插件接口与页面 | #2616 已实现，见 RFC #2579 设计 | — |
| 热加载、数据升级、MCP 工具 | 未实现，见 RFC #2579 设计 | — |
| 结构化可观测性字段 | 未实现 | §12 |
| Source 插件、采集、移除旧 Source 插件框架 | 未实现，由 RFC #2617 规定 | — |

---

## 3. 模块与文件

| 文件 | 职责 |
| --- | --- |
| `src/powercontext/plugins.py` | 公共契约：`ArtifactPlugin`、`ArtifactPluginSpec`、`Inputs`、`Interval`、`Cron`、`PluginContext`、读写接口、请求与结果模型、`Rendering`、`SearchRequest` / `SearchResult`、`PluginApi`、`Route`、`PluginPages`、`ApiError`、`InvalidContent`、`ModelUnavailable` |
| `src/powercontext/builtin/plugins/manifest.py` | 导入 `plugin.py`、校验声明、计算代码指纹、导出 `PluginManifest`；作为检查子进程的入口 |
| `src/powercontext/builtin/plugins/executor.py` | Worker 中的 `generate` 执行器：窗口、Projection、游标与确认 |
| `src/powercontext/builtin/plugins/writer.py` | 写入器、写事务块、读取视图、用户修改模式、`lineage_only` Source、插件 family 的合并服务 |
| `src/powercontext/builtin/plugins/host.py` | 宿主进程入口：重建数据库连接与权限服务、导入插件、调用 `setup`、处理用户修改、展示、检索、Source 写入和接口请求；`ctx` 的请求辅助能力 |
| `src/powercontext/builtin/plugins/host_protocol.py` | `PluginHostSpec` 与 Server 和宿主进程之间经 `Pipe` 传递的消息 |
| `src/powercontext/builtin/plugins/storage.py` | 专属存储与模型的实现 |
| `src/powercontext/builtin/plugins/families.py` | 插件 family 注册、revision `metadata` |
| `src/powercontext/server/plugins.py` | `PluginManager`：检查、注册、binding、宿主进程准备、`/v1/plugins` 视图 |
| `src/powercontext/server/plugin_hosts.py` | Server 侧的宿主进程管理：启动、转发、超时终止、退避重启、停止 |
| `src/powercontext/server/app.py` | 插件 family 的新建、替换、合并、遗忘、恢复、展示和检索转交宿主进程；错误映射；`/v1/plugins` 接口；插件路由 |
| `src/powercontext/builtin/runtime/prepared_context.py` | Prepared Context 中插件 family 的章节 |
| `src/powercontext/builtin/persistence/migrations/` | 迁移 `v1_5_0_005`（SQLite、MySQL）与插件数据的就绪检查 |
| `openapi/powercontext.yaml` | 插件相关接口；修改后执行 `make api-generate` 与 `make contract-test` |

---

## 4. 声明、检查与加载

### 4.1 检查子进程

`check_plugin_directory` 用 `python -m powercontext.builtin.plugins.manifest <目录>` 启动检查子进程，时限 60 秒。子进程：

1. 校验目录名匹配 `^[a-z][a-z0-9-]{0,63}$`，导入 `plugin.py`（模块名 `powercontext_plugin_<名字>`，插件目录加入 `sys.path`）；
2. 确认模块级变量 `plugin` 是 `ArtifactPlugin`，`setup` 是 async 函数；
3. 用 `ArtifactPluginSpec` 校验声明；声明了 `pages` 时确认 `entry` 是 `directory` 内的文件；为每个 Source 定义生成声明式
   清单；
4. 计算代码指纹（§4.3）；
5. 把 `PluginManifest` 以一行 JSON 输出到 stdout；失败时只输出 `PluginConfigError` 的消息或异常类型名。

Server 按目录名顺序逐个检查插件。检查通过后，family 与已加载插件重复、Source 类型与内置类型重复，都判为加载失败。

### 4.2 插件记录

```python
class PluginManifest(BaseModel):            # 可序列化，跨进程传递
    name: str
    spec: ArtifactPluginSpec                # 不含 setup、definitions 和路由的模型类
    fingerprint: str                        # 代码指纹
    source_manifests: tuple[dict, ...]      # Source 定义的声明式清单

@dataclass
class _Plugin:                              # PluginManager 中的记录
    name: str
    directory: Path
    manifest: PluginManifest | None
    error: str | None                       # 检查或注册失败的原因
    tasks: list[asyncio.Task]               # 本插件的后台任务
    host: PluginHost | None                 # 宿主进程句柄，见 §7
```

`PluginManager.plugins: dict[str, _Plugin]` 是每个 Server 进程的唯一记录，不持久化，Server 启动时按插件目录重建。插件
状态为 `failed` 的条件是检查失败，或宿主进程报告了永久失败（例如 `setup` 返回的对象缺少必需方法）。

### 4.3 代码指纹

```python
def code_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(root).parts):
        digest.update(str(path.relative_to(root)).encode()); digest.update(b"\0")
        digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()
```

- 只按文件内容计算，不使用大小和修改时间。
- Worker 和宿主进程导入插件时重新执行 `load_plugin`，得到的 `PluginManifest`（含指纹）与 Server 检查时的不一致就失败，提示
  重启 Server。
- 指纹随每个插件写入的 revision 记录在 `pc_artifacts.metadata` 中（§10）。

### 4.4 注册

检查通过后，`PluginManager._register` 依次：

1. 把 Source 定义清单登记到 `pc_source_definition_manifests`；与已登记清单完全相同时复用，内容不同则失败；
2. `register_plugin_family(family)`；
3. 在合并注册表中登记该 family 的合并服务，写入的 revision 带上插件名和指纹；
4. 配置了 `inputs` 时 `register_source_binding`，让 Source 写入把该插件的 binding 标为待处理；
5. 配置了 `schedule` 或 `on_sources`、且本实例派发 Worker 时，向 Supervisor 注册 binding。

binding 名为 `plugin:<name>`。`on_sources` 映射为 2 秒的 `automatic_processing_interval`；`Interval` 与 `on_sources` 同时
配置时取较小值；`Cron` 映射为 binding 的 cron。没有 `inputs` 的插件由 `_tick` 任务按 `Interval` 为 `scopes` 中的每个
Scope 写入调用意图。

`api` 角色的实例注册 family 和 Source 类型，不注册 binding；`background` 角色的实例注册 binding，但不接收请求，也不启动
宿主进程。

---

## 5. Worker 执行器

```text
Worker 收到 (plugin:<name>, scope_id, request_generation)
  -> 短事务：ScopeInvocation.start；读取 (scope_id, plugin:<name>) 的游标
     -> 配置了 inputs：读取 journal 高水位，确定窗口 (after, through]，through ≤ after + max_sources，读取窗口
  -> 按 inputs.source_types 过滤，按每条 Source 的类型计算 Projection（projections 中的类型专用值，否则为 projection）
  -> 窗口为空且配置了 inputs：短事务完成游标 CAS 与确认，不加载插件
  -> 重新 load_plugin 并核对 PluginManifest；打开专属存储和模型；调用 setup；调用 generate（受 timeout_seconds 约束）
     -> 事务外读取：独立只读短事务，记录读到的 revision（§6.3）
     -> 写事务块开始：开启事务；guard（租约与调用意图）
        块内写操作在该事务中执行
        块结束：游标 CAS 到 through；确认（是否还有剩余工作）；提交
  -> generate 返回且未开启写事务块：短事务完成游标 CAS 与确认
  -> 写事务块提交后 generate 抛出异常：记录为提交后错误，不重试该窗口
```

- 窗口内类型不匹配的 Source 被过滤，游标仍推进过整个窗口；匹配类型的 Source 缺少所需 Projection 时调用失败。
- 游标 CAS 以读取时的 `generation` 为期望值，保证领导权变更或重复派发时同一窗口只提交一次。
- 配置了 `scopes` 时，Scope 不在名单中的调用在授权时失败。
- 待处理工作的发现：Server 进程中的 Source 写入把关注该 Scope 的插件 binding 标记为待处理；`SourceProcessingPendingProvider`
  按 journal 高水位与游标的差值补齐其他进程写入的 Source。
- Worker 打开数据库时不创建表，schema 由 Server 准备。

---

## 6. 写入器

### 6.1 写操作与复用的 core 实现

| 写操作 | 复用的实现 |
| --- | --- |
| `put` | 共享合并引擎的修改路径，新建时走 `ArtifactRepository` 的创建 |
| `revert` | 共享恢复流程 |
| `set_lifecycle` | `deprecated` / `active` 走共享的遗忘与恢复流程；`retired` 与指定替代者使用治理状态转换 |
| `merge` | 共享合并引擎（RFC #1859） |
| `preview_undo_merge` / `undo_merge` | 共享合并引擎的撤销预览与恢复 |
| `bind_handle` | Artifact handle 绑定 |
| `set_tags` | tags 替换 |

读取视图提供 `get`、`revisions`、`list`、`by_source`、`merge_inputs`、`tags`、`resolve_handle`。

### 6.2 内容不变时的写入

- 后台调用中，`put` 的内容与当前 revision 相同则不写入，返回当前快照，传入的 `source_refs` 和 `artifact_refs` 被忽略。
  这与 Atomic Memory 的 reconcile 一致：`noop` 决策不产生新 revision。
- 用户修改模式中，`put` 总是追加 revision，与内置 family 的替换一致。

### 6.3 事务外读取的 revision 校验

写入器维护 `observed: dict[str, int]`：事务外读取每返回一个当前快照，就记录其 `artifact_id → revision`；同一 ID 读到多次时
保留最早的一次。写事务块中的 `put` 没有给出 `expected_revision` 时，若该 ID 在 `observed` 中，就以记录的 revision 作为期望值；
不在其中则不校验。读取指定历史 revision 不记录。

### 6.4 写入范围与上限

| 规则 | 实现 |
| --- | --- |
| 只写本 Scope、本 family | 写操作的 Scope 与 family 固定在写入器上，参数中没有这两项 |
| `source_refs` 范围 | `generate` 中限于窗口内的 Source；用户修改中限于目标 Artifact 当前 lineage 中的 Source |
| `artifact_refs` 范围 | 本 Scope 中任意 family 实际存在的 revision |
| 单块操作数 | 1000 |
| 单份内容大小 | 256 KiB（UTF-8 JSON 序列化后） |
| 单块总字节数 | 8 MiB |
| 写事务块时限 | `transaction_timeout_seconds`，默认 30 秒 |
| 每次调用的写事务块数 | 1；第二次开启抛出异常 |

超出上限即抛出异常并回滚，不截断。

### 6.5 用户修改模式

宿主进程为用户修改创建写入器时传入 `UserChange(operation, artifact_id, inputs)`：

- 写事务块内只允许 §8.2 表中约定的那一项写操作，以及对同一 Artifact 的 `set_tags`；其他写操作抛出 `PluginWriteError`。
- 块结束时检查约定的写操作恰好完成一次，否则回滚并抛出 `PluginContractError`，映射为 502。
- 写操作以调用者身份授权，使用共享的 Artifact 访问策略。
- 新建和编辑时，写入器在同一事务中把调用方提交的内容保存为 `lineage_only` Content Source（§8.3）。

---

## 7. 插件宿主进程

### 7.1 进程模型

宿主进程是 **core 运行时加插件**：与 Worker 一样从配置自建数据库连接、权限服务和所需的 core 服务，在此之上加载插件。
Server 进程只负责认证调用者和转发请求，鉴权、版本预检和写入都在宿主进程中完成。

- 每个 Server 进程为每个加载成功的插件维护一组宿主进程，不同插件不共用进程。加载后立即在后台启动第一个，不等第一个
  请求，并一直保留；请求到达时尚未就绪则等待，等待时间计入请求时限。
- 进程组按负载增减：本插件所有进程的并发槽位都占满、且有请求排队时，再启动一个，直到本插件的上限；多出的进程空闲 5 分钟
  后发送 `HostShutdown` 停止。本插件的上限取声明中的 `host_processes`，`POWERCONTEXT_SERVER_PLUGIN_HOST_PROCESSES`（按
  插件名的 JSON）可以覆盖；所有插件的宿主进程总数不超过 `POWERCONTEXT_SERVER_PLUGIN_TOTAL_HOST_PROCESSES`，达到总上限时
  不再为负载启动新进程，请求在已有进程上排队；每个插件常驻的第一个进程不受总上限限制，使每个加载成功的插件都可用。
  （两个变量名互不为前缀：Server 设置以 `_` 作为嵌套分隔符，`..._HOST_PROCESSES_TOTAL` 会被读成前者 JSON 中的键。）请求分给进行中请求最少的进程。
- 启动方式：`multiprocessing.get_context("spawn")` 启动全新解释器，不继承 Server 进程已导入的模块。
- 启动参数 `PluginHostSpec` 可以 pickle：数据库配置、插件目录、`PluginManifest`、权限配置、推理配置、所有已加载插件的
  family 和 Source binding、专属存储的根目录。
- 启动步骤：
  1. `load_plugin` 并核对 `PluginManifest`；
  2. 注册所有已加载插件的 family 和 Source binding，使读取和 Source 写入与 Server 进程一致；
  3. 打开数据库连接（不建表），用 `open_worker_security` 重建权限服务；
  4. 打开专属存储和模型；载入 Source 定义和路由表；
  5. 调用一次 `setup(ctx)`（`ctx.scope_id` 为 `None`）；
  6. 检查必需方法：`render_artifact`，以及配置了 `schedule` 或 `on_sources` 时的 `generate`；缺少时发送
     `HostFailed(permanent=True)`，插件为 `failed`，不再重启；
  7. 发送 `HostReady(implemented=...)`，带上已实现的用户修改方法。
- 宿主进程是单个 asyncio 事件循环，可以并发处理多个请求；Server 侧以信号量限制每个宿主进程的并发请求数为 8，超出的请求
  排队，排队时间计入请求时限。宿主进程每秒发送一次 `HostHeartbeat`，由事件循环中的任务发出，事件循环被阻塞时心跳随之
  中断。

### 7.2 通信

Server 与宿主进程之间是一条双向 `multiprocessing` `Pipe`，消息是可 pickle 的 dataclass，`Pipe` 负责分帧。插件写到
stdout / stderr 的内容不进入这条通道，bytes 可以直接放在消息里。

```python
HostMethod = Literal["create_artifact", "update_artifact", "merge_artifacts", "forget_artifact", "restore_artifact",
                     "create_source", "render_artifact", "search_artifacts", "api"]

@dataclass(frozen=True)
class HostRequest:
    id: str
    method: HostMethod
    scope_id: str | None
    caller: Any                  # Server 认证得到的调用者；未启用访问控制时为 None
    audit: Any                   # HTTP 请求的审计上下文
    payload: dict[str, Any]
    body: Any = None             # 插件接口的请求体，可以是 bytes

@dataclass(frozen=True)
class HostResponse:
    id: str
    outcome: Literal["ok", "invalid_content", "not_implemented", "forbidden", "not_found", "precondition_failed",
                     "conflict", "contract_violation", "api_error", "model_unavailable", "error"]
    result: Any = None
    message: str | None = None   # invalid_content、api_error 时携带插件给出的消息
    field: str | None = None     # invalid_content 的字段；api_error 的错误码
    status: int | None = None    # api_error 的状态码
    error_type: str | None = None
```

另有 `HostReady`、`HostFailed`、`HostShutdown`、`HostHeartbeat` 和 `HostCancel(id)` 控制消息。Server 端的 `PluginHost` 在后台任务中读取 `Pipe`，按 `id`
把响应交给等待的请求。

### 7.3 超时、故障与重启

| 情况 | 处理 |
| --- | --- |
| 单个请求超过时限 | Server 发送 `HostCancel(id)`，宿主进程取消该请求的任务；写事务随之回滚，该请求返回 504，其他请求不受影响 |
| 心跳中断超过 5 秒 | 视为失去响应：终止该宿主进程（`terminate`，5 秒后 `kill`）；已超时的请求返回 504，其他进行中的请求返回 503 |
| 进程退出或 `Pipe` 关闭 | 进行中的请求返回 503 |
| 就绪等待超过请求时限 | 该请求返回 503 |
| 重启 | 退避后在后台重新启动；退避从 1 秒开始，每次翻倍，上限 60 秒；连续运行 5 分钟后清零 |
| 退避期间 | 本插件没有其他可用进程时，同步调用返回 503；后台调用不受影响 |
| 永久失败（`HostFailed(permanent=True)`） | 不重启，插件为 `failed` |
| Server 停止 | 发送 `HostShutdown`，等待至多 10 秒，然后终止 |

同步调用的时限：用户修改、展示和检索使用声明中的 `change_timeout_seconds`，插件接口使用路由的 `timeout_seconds`。时限从
Server 收到请求开始计算，包括等待就绪和排队的时间。

### 7.4 宿主进程中的写入

宿主进程中的写入器与 Worker 使用同一实现，区别是：没有游标和调用确认，写事务块结束时只提交本次写入；授权使用请求携带的
调用者；写事务块之外的读取同样记录 `observed`。

---

## 8. 同步调用

### 8.1 用户修改

```text
HTTP 请求（新建 / PUT / 合并 / 遗忘 / 恢复）
  -> Server：认证；按 family 找到插件和宿主进程
  -> Server：把 HostRequest（调用者、审计上下文、请求内容）交给宿主进程
  -> 宿主进程：插件未实现对应方法 → not_implemented
  -> 宿主进程：鉴权（与内置 family 相同）→ forbidden
  -> 宿主进程：版本预检，不合格时不调用插件
       PUT：revision 与 If-Match 不符 → precondition_failed
       合并、遗忘：revision 或 state_version 不符 → conflict
  -> 宿主进程：新建与合并时未指定 ID 则分配 artifact_id / into
  -> 宿主进程：构造请求模型，调用插件方法；写入器完成约定的写操作；提交时被其他写入改变则回滚并返回 conflict
  -> 宿主进程：方法返回但写事务未提交 → contract_violation
  -> Server：按 HostResponse 映射 HTTP 响应（§8.7）
```

### 8.2 约定的写操作

| 方法 | 约定 |
| --- | --- |
| `create_artifact` | 恰好一次 `put(request.artifact_id, ...)`，目标此前不存在 |
| `update_artifact` | 恰好一次 `put(request.artifact_id, ..., expected_revision=request.expected_revision)` |
| `merge_artifacts` | 恰好一次 `merge(输入 ID, into=request.into, ...)`，输入与请求一致 |
| `forget_artifact` | 恰好一次 `set_lifecycle(request.artifact_id, "deprecated")` |
| `restore_artifact` | `restore`：恰好一次 `revert` 或 `set_lifecycle(..., "active")`；`undo_merge`：恰好一次 `undo_merge(request.artifact_id)` |

### 8.3 溯源

| 操作 | Content Source 的 `operation` | 目标 revision |
| --- | --- | --- |
| 新建 | `artifact_create` | 1 |
| 编辑 | `artifact_replace` | 新 revision |

Content Source 的内容为调用方提交的内容，`role="lineage_only"`，与内置 family 的写法相同，不进入任何插件的输入窗口。
遗忘、恢复和合并沿用共享治理流程记录的关系，不另写 Content Source。

### 8.4 展示

`GET /v1/scopes/{scope_id}/artifacts/{family}/{artifact_id}/rendering`（可带 `revision`）：宿主进程检查调用者的
`scope.read`，读取指定 revision，调用 `render_artifact`，返回 `Rendering`（Markdown 或纯文本）和对应的 revision 引用；
Artifact 不存在时 404。

### 8.5 检索与 Prepared Context

- 插件 family 的通用检索请求原样转交宿主进程（不含 `include_scores`），插件自行解释全部字段；插件未实现
  `search_artifacts` 时返回 422。
- 宿主进程检查调用者的 `scope.read`，然后逐条核对插件返回的命中：family 必须是本插件的，否则为 `contract_violation`；
  只保留 revision 等于当前 revision 且状态为 `active` 的命中，保持插件给出的顺序。
- Server 按调用者的读取权限组装响应，是否带分数由 `include_scores` 决定。
- Prepared Context 中插件不可用（宿主进程不可用、超时或检索失败）时，省略该 section，在回执的检索记录中把该 family 记为 `failed`，其他
  section 照常返回。权限过滤后的命中可能少于 `limit`，core 不补取。
- `POST /v1/context/prepare` 的章节可以列出实现了检索的插件 family：core 以请求的查询和章节的 `limit` 调用
  `search_artifacts`，章节的 `search` 中的其他通用检索字段（如 `mode`、过滤条件）原样透传，同时传入 `max_bytes`。既不是
  内置 family、也不是实现了检索的插件 family 时返回 422。
- core 不为插件 family 建立索引。

### 8.6 插件自带的 Source 类型

- 检查子进程为每个定义导出声明式清单，加载时登记到 `pc_source_definition_manifests`（§4.4）。
- `POST /v1/scopes/{scope_id}/sources` 指定 `source_type` 为插件类型时，Server 交给宿主进程（`create_source`）：检查调用者的
  `scope.contribute`，用 `input_class` 校验内容，执行 `resolve` 与 Projection 并保存，沿用身份去重。
- 插件路由中的 `ctx.sources.submit` 走同一条保存路径。

### 8.7 错误映射

| `HostResponse.outcome` / 情况 | HTTP | 错误码 |
| --- | --- | --- |
| `ok` | 与内置 family 相同（新建 201，其他 200） | — |
| `invalid_content` | 422，带插件的消息和字段 | — |
| `not_implemented` | 422 | `plugin_operation_not_supported` |
| `forbidden` | 403 | `access_denied` |
| `not_found` | 404 | `not_found` |
| `precondition_failed` | 412 | `precondition_failed` |
| `conflict` | 409 | `conflict` |
| `contract_violation` | 502 | `plugin_contract_violation` |
| `model_unavailable` | 503 | `model_unavailable` |
| `api_error` | 插件给出的 4xx | 插件给出的错误码 |
| `error` | 500，只记录异常类型 | `plugin_error` |
| 请求超过时限 | 504 | `plugin_timeout` |
| 宿主进程不可用、退避中、因失去响应被终止时的其他请求 | 503 | `plugin_unavailable` |

---

## 9. 专属存储与模型

### 9.1 专属存储

| 后端 | 位置 |
| --- | --- |
| SQLite | `<数据目录>/plugins/<插件名>.sqlite3`，首次调用 `engine()` 时创建 |
| OceanBase / seekdb | 未提供；`ctx.storage` 抛出 `RuntimeError` |

- `engine()` 返回 `AsyncEngine`，插件自己建表和迁移。
- Worker 中每次调用创建，调用结束时释放；宿主进程中在进程存续期间复用。
- 移除插件不删除这块存储。

### 9.2 模型

`ctx.models` 由宿主进程或 Worker 根据 Server 的推理配置构造，复用 core 的模型客户端：

- `embed(texts)` 调用 Server 配置的 embedding 模型，`embedding_dimension` 给出维度；
- `generate(messages, max_tokens=None)` 调用 Server 配置的生成模型，沿用生成超时和模型设置；
- 对应模型未配置时抛出 `ModelUnavailable`，同步调用映射为 503；两种模型都未配置时 `ctx.models` 为 `None`；
- 推理配置经 Worker spec 和宿主进程启动参数传递，插件代码拿不到其中的地址和凭据。

---

## 10. 持久化与迁移

迁移 `v1_5_0_005`（SQLite 与 MySQL）：

| 变更 | 对象 |
| --- | --- |
| 删除表 | #2246 框架的 `pc_artifact_family_schemas`，以及声明式索引的 `pc_artifact_search_fts`、`pc_artifact_search_text`、`pc_artifact_vectors`、`pc_artifact_index_changes`、`pc_artifact_index_coverage`、`pc_artifact_search_states`、`pc_artifact_vector_indexes`、`pc_artifact_vector_spaces` |
| 删除列 | `pc_artifact_processing_binding_states` 的 `plugin_id`、`plugin_family`、`plugin_spec`、`binding_generation`、`desired_state`、`phase`、`last_error` |
| 新增列 | `pc_artifacts.metadata`（可空）：插件写入的 revision 记录 `{"plugin", "version"}`，`version` 为代码指纹 |

- 只在这些表和列没有数据时删除；有数据时迁移预检以 `unsupported_plugin_data` 拒绝，部署者先在旧版本上导出并移除 #2246
  插件。已发布版本的迁移资源不变。
- 就绪检查接受 `plugin:` 前缀的 binding：其待处理调用意图由插件目录决定，插件移除后遗留的意图不再处理。
- 读取 revision 时不返回 `metadata`，使早于本版本的迁移步骤仍可运行。
- 可移植导出中的 `artifact_revision` 记录升到 schema 版本 3，仍可恢复版本 2。
- 不受影响：`pc_sources`、Projection、journal、`pc_source_definition_manifests`、`pc_artifacts` 的其他列及 heads、lineage、
  合并记录、handle、tags；`pc_source_cursors`；Supervisor 的调用意图、pending、租约表。
- 旧 Source 插件框架（管理服务、`pc_source_write_plugins`）保留，由 RFC #2617 的实现移除。

---

## 11. 公开读取

- 插件 family 的 Artifact 由 core 保存在 `pc_artifacts` 中，`content` 原样保存。列出、读取、revision 历史、治理状态和
  tags 由 core 直接提供，不调用插件。
- 插件可以把大块内容放在专属存储中，Artifact 只记录摘要或引用；完整内容由插件接口提供。
- 移除插件后，其 family 不再注册，通用接口返回 404；数据保留。

---

## 12. 可观测性

当前实现通过 `log_safely` 输出插件加载失败、宿主进程启动、终止与重启、提交后错误等事件，消息只包含插件名和异常类型。
结构化字段尚未登记，计划如下：

| 字段 | 含义 |
| --- | --- |
| `plugin_name` | 插件名 |
| `plugin_method` | `generate`、`render_artifact`、`search_artifacts`、`create_source`、用户修改方法名、`api` |
| `plugin_process` | `worker` 或 `host` |
| `trigger` | `schedule`、`sources`；同步调用为 `request` |
| `operation_count` | 本次提交的写操作数 |
| `transaction_ms` | 写事务块从开始到提交或回滚的耗时，毫秒 |

- 宿主进程的终止原因：`timeout`、`crash`、`server_stop`。
- 指标标签使用 `plugin_name`、`plugin_method`、`plugin_process`，不使用 Scope ID。
- 日志不包含 Source 内容、Projection、Artifact 内容和环境变量值；插件异常只记录类型。
- 新字段登记到允许列表，并在中英文可观测性文档中说明。

---

## 13. 默认值汇总

| 项 | 默认值 | 配置 |
| --- | --- | --- |
| 检查子进程时限 | 60 秒 | 固定 |
| 写事务块时限 | 30 秒 | 声明 `transaction_timeout_seconds` |
| 后台生成时限 | 300 秒 | 声明 `timeout_seconds` |
| 用户修改、展示、检索时限 | 30 秒 | 声明 `change_timeout_seconds` |
| 宿主进程并发请求数 | 8 | 固定 |
| 每个插件的宿主进程上限 | 1 | 声明 `host_processes`；`POWERCONTEXT_SERVER_PLUGIN_HOST_PROCESSES` |
| 宿主进程总上限 | 不设；每个插件的第一个进程不计入限制 | `POWERCONTEXT_SERVER_PLUGIN_TOTAL_HOST_PROCESSES` |
| 多出的宿主进程空闲停止 | 5 分钟 | 固定 |
| 心跳间隔 / 失去响应判定 | 1 秒 / 5 秒 | 固定 |
| 宿主进程就绪等待 | 60 秒 | 固定 |
| 宿主进程重启退避 | 1 秒起，翻倍，上限 60 秒；运行 5 分钟后清零 | 固定 |
| 宿主进程停止等待 | 10 秒 | 固定 |
| 写事务块操作数 / 单份内容 / 总字节 | 1000 / 256 KiB / 8 MiB | 固定 |
| `on_sources` 接纳间隔 | 2 秒 | 固定 |

---

## 14. 后续工作

| 工作 | 依据 |
| --- | --- |
| 热加载、版本快照、数据升级 `upgrade`、MCP 工具 | RFC #2579，见其设计 |
| OceanBase / seekdb 的专属存储 | 本文 §9.1 |
| 结构化可观测性字段 | 本文 §12 |
| Source 插件、采集目标与 runner、版本核对、移除旧 Source 插件框架 | RFC #2617 |
| 插件安装与依赖：按包名、Git 地址或 wheel 安装，按插件隔离环境，中心与设备分别声明依赖 | 后续提案 |

---

## 15. 待明确事项

| 事项 | 说明 |
| --- | --- |
| OceanBase / seekdb 上的专属存储 | 按插件建库需要建库权限；没有权限时是否退回到 core 库中按插件名前缀建表 |
| 宿主进程的默认参数 | 并发上限、心跳时限、总上限、退避参数需要用真实插件压测后确认 |
| 检查的并发 | 当前按目录顺序逐个检查，每个约 0.6 秒加插件自身导入时间；插件较多时是否并发检查 |

---

## 16. RFC 之外的设计决定

RFC 只规定契约。下表列出本文为实现契约所做、RFC 没有规定的决定。

| # | 决定 | 理由 |
| --- | --- | --- |
| D1 | 宿主进程用 `multiprocessing` 的 spawn 启动，Server 与宿主进程之间通过双向 `Pipe` 传递可 pickle 的消息（§7.1、§7.2） | 与现有 Worker 启动方式一致；插件写 stdout 不会破坏通信；bytes 可以直接传递；跨平台。stdin / stdout 按行传 JSON 需要自己分帧，且会被插件的输出破坏 |
| D2 | 宿主进程是"core 运行时 + 插件"：像 Worker 一样从配置自建数据库连接和权限服务，注册所有已加载插件的 family 和 Source binding；Server 只认证调用者并转发请求（§7.1） | 数据库连接不能跨进程传递；鉴权、Scope 管理和写入在同一进程中完成，不需要回调 Server。代价是每个宿主进程导入整套 core，约 5.5 秒、150 MB |
| D3 | 用户修改的鉴权和版本预检在宿主进程中执行，不合格时不调用插件（§8.1） | 随 D2 |
| D4 | 宿主进程在 `setup` 之后检查必需方法，缺少时永久失败、不重启；同时报告已实现的用户修改方法（§7.1） | 方法在 `setup` 返回的对象上，只有宿主进程知道；缺少方法是插件的问题，重启无效 |
| D5 | 插件按目录顺序逐个检查（§4.1） | 实现简单，启动顺序确定；插件较多时再考虑并发（§15） |
| D6 | 事务外读取记录同一 Artifact 最早读到的 revision，读取历史 revision 不记录（§6.3） | 最早一次读取是插件做决定的起点 |
| D7 | 已登记的 Source 定义清单与插件给出的完全相同时视为同一定义，内容不同才算冲突（§4.4） | 沿用现有清单登记逻辑；同一插件在多个 Server 实例上加载时依赖这一行为 |
| D8 | 专属存储的 engine 在 Worker 中每次调用创建，在宿主进程中复用（§9.1） | 与 `setup` 返回对象的生命周期一致 |
| D9 | 请求超时时先取消该请求，只在心跳中断（事件循环被阻塞）时终止宿主进程；超时的请求返回 504，被连带终止的请求返回 503（§7.3） | 插件等待 I/O 时取消可以生效，不必牺牲同一进程中的其他请求；只有阻塞事件循环时取消无法生效，此时进程中的其他请求本来也已无法完成 |
| D10 | 每个加载成功的插件都在后台预先启动一个宿主进程并一直保留，负载高时按需增加、空闲后减少，设总上限（§7.1） | 宿主进程启动需要数秒，不应由第一个请求承担；每个插件都有必需的 `render_artifact`，都需要宿主进程。每个插件一组进程而不共用，使阻塞或崩溃不波及其他插件，也为以后按插件隔离依赖留出可能；总上限控制内存 |
| D11 | 插件写入的 revision 在 `metadata` 中记录插件名和代码指纹（§10） | 排查问题和以后的升级可以知道每个 revision 由哪个版本写入；不进入 Artifact 内容，不影响身份和去重 |
| D12 | 检索结果只保留当前、`active` 的 revision，按插件给出的顺序（§8.5） | 插件索引可能落后于 core 数据；以 core 的当前状态为准，不让过期或已遗忘的内容出现在结果中 |
