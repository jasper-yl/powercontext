# 系统设计：插件热加载与自定义接口（Plugin Reload and APIs）

| 项 | 内容 |
| --- | --- |
| 设计源 | [RFC #2579 插件热加载与自定义接口](https://github.com/oceanbase/powercontext/pull/2579) |
| 前置设计 | [插件接口设计](2549_plugin_interfaces_design.md)（RFC #2549） |
| 实现 | 插件接口与页面：[oceanbase/powercontext#2616](https://github.com/oceanbase/powercontext/pull/2616)；热加载、数据升级、MCP 工具：后续 |
| 日期 | 2026-11-13 |
| 技术栈 | Python 3.11+ / pydantic v2 / FastAPI / Starlette；Dashboard 为服务端渲染页面加少量脚本 |

本文是 RFC #2579 的实现设计，建立在 RFC #2549 的实现设计之上：宿主进程、已激活插件记录、写入器和用户修改模式都沿用
该文档，本文只描述新增和改变的部分。两者冲突时以 RFC 为准，并修订本文。插件要实现的接口总表见 RFC #2549 设计第 0 节。

| 部分 | 状态 | 章节 |
| --- | --- | --- |
| 插件路由、二进制请求体与响应、处理函数的能力 | #2616 已实现 | §5.1–§5.3、§6 |
| 插件页面 | #2616 已实现 | §7 |
| 扫描、版本快照、切换（热加载） | #2638 已实现 | §2–§4.6 |
| 数据升级 `upgrade` | #2638 已实现 | §4.7 |
| 插件接口描述（OpenAPI） | #2638 已实现 | §5.4 |
| MCP 工具 | #2638 已实现 | §5.5 |
| `/v1/plugins` 的版本字段与结构化可观测性 | #2638 已实现；插件接口的指标未实现 | §4.6、§8 |

---

## 0. 不可违反的约束

| # | 约束 | 实现上的保证 |
| --- | --- | --- |
| C1 | 一次调用只使用一个版本的插件代码 | 子进程只从只读的版本快照导入；快照在被使用期间不删除 |
| C2 | 版本标识能识别任何内容变化 | 版本摘要按文件内容计算，不使用大小和修改时间 |
| C3 | 插件接口的授权由 core 完成 | 宿主进程中的 core 代码先按 `access` 授权，`ctx` 中的每个读写能力再按调用者身份检查 |
| C4 | 插件页面借不到用户会话 | 页面运行在不透明来源；宿主页面只转发给未跳转过的 iframe，且只转发到本插件声明的路由 |

---

## 1. 模块与文件

| 文件 | 状态 | 职责 |
| --- | --- | --- |
| `src/powercontext/plugins.py` | 已实现 | `PluginApi`、`Route`、`ApiRequest`、`BinaryResponse`、`ApiError`、`Caller`、`PluginPages`、`PluginSources`、`PluginScopes` |
| `src/powercontext/server/app.py` | 已实现 | `call_plugin_api`：路由匹配、请求体限制、转交宿主进程、响应映射 |
| `src/powercontext/builtin/plugins/host.py` | 已实现 | 处理 `api` 请求：Scope 授权、`request_model` 校验、调用处理函数；在本地提供 `ctx.require`、`ctx.reads`、`ctx.writer`、`ctx.sources`、`ctx.scopes` |
| `src/powercontext/builtin/plugins/host_protocol.py` | 已实现 | `api` 请求与响应 |
| `src/powercontext/server/dashboard/plugin_pages.py` | 已实现 | 导航项、宿主页面、静态文件、服务端转发、`_client.js` |
| `src/powercontext/server/dashboard/templates/plugin.html` | 已实现 | 宿主页面与其脚本：iframe 管理与消息转发 |
| `src/powercontext/server/dashboard/static/plugin-client.js` | 已实现 | 提供给插件页面的客户端脚本 |
| `src/powercontext/server/plugin_versions.py` | 已实现 | 版本摘要、快照生成与清理；扫描循环与切换在 `server/plugins.py` |
| `src/powercontext/server/plugins.py` | 待修改 | `PluginManager` 增加新增、更新、移除的切换逻辑；插件记录增加版本信息 |
| `src/powercontext/server/plugin_hosts.py` | 待修改 | 宿主进程替换：新进程就绪后切换，旧进程排空后停止 |
| `openapi/powercontext.yaml` | 待修改 | 插件接口描述；`/v1/plugins` 的版本字段 |

---

## 2. 扫描

### 2.1 扫描循环

```text
每个 Server 进程，每隔 rescan 秒（以及启动时）：
  -> 列出插件目录的直接子目录
  -> 对每个目录在线程池中计算版本摘要（§2.2）
  -> 摘要与上次扫描相同，且与当前版本相同：跳过
  -> 摘要与上次扫描不同：记为待稳定，本轮不检查
  -> 摘要连续两次扫描相同，且与当前版本不同：生成版本快照（§3），从快照检查
  -> 根据检查结果执行新增、更新或卸载（§4）
  -> 本轮之前存在、本轮消失的目录：卸载
  -> 清理不再使用的快照（§3.4）
  -> 记录 scan_ms
```

- 扫描循环是 Server 进程中的一个 asyncio 任务；摘要计算放在线程池中，不阻塞事件循环。
- 启动时的第一次扫描不等待稳定，直接为每个目录生成快照并检查。
- 同一时刻只有一轮扫描在执行；上一轮未结束时跳过本次触发。
- `POWERCONTEXT_SERVER_PLUGINS_RESCAN_SECONDS` 为 0 时不启动扫描循环，插件从插件目录直接加载，并按 RFC #2549 的规则核对
  代码指纹。

### 2.2 版本摘要

版本摘要与 RFC #2549 的代码指纹使用同一算法：对目录下所有文件（不含 `__pycache__`）按相对路径排序，依次把相对路径和
文件内容送入 SHA-256。

在 2 核开发机上，对 14 MB、676 个文件（`src/powercontext`，文件在页缓存中）计算一次约 230 ms；只读元数据约 33 ms。插件目录
不放依赖，通常远小于此。

### 2.3 每个目录的扫描状态

```python
@dataclass
class ScanState:
    last_digest: str | None      # 上一轮扫描得到的摘要
    stable_rounds: int           # 连续相同的轮数
    failed_digest: str | None    # 最近一次检查失败的摘要，避免对同一内容反复检查
```

同一摘要检查失败后，在内容变化之前不再检查。

---

## 3. 版本快照

### 3.1 布局

```text
<数据目录>/plugin-versions/
└── <插件名>/
    └── <版本摘要>/           # 只读
        ├── plugin.py
        └── ...
```

### 3.2 生成

1. 复制到 `<数据目录>/plugin-versions/<插件名>/.tmp-<随机串>/`，跳过 `__pycache__`；
2. 对临时目录重新计算摘要，与扫描得到的摘要不一致则放弃本轮（复制期间目录又被修改）；
3. 把文件设为只读（`0444`）；目录保持所有者可写（`0755`），删除数据目录时普通工具仍能删掉快照；
4. 改名为 `<版本摘要>/`；目标已存在时删除临时目录，直接使用已有快照。

### 3.3 使用

- 检查子进程、宿主进程、Worker 都以快照目录为插件目录：快照目录加入 `sys.path`，`ctx.plugin_dir` 指向快照。
- `PluginWorkerSpec` 增加 `snapshot` 字段；Supervisor binding 的 launcher 在派发时读取当前快照，因此切换版本后新派发的
  Worker 自动使用新快照，不需要重新注册 binding。
- 使用快照时不再核对代码指纹：快照只读，且路径中的摘要就是其内容。

### 3.4 引用与清理

扫描末尾删除本进程创建、且满足以下条件的快照：不是任何插件的当前版本；没有本进程中正在排空的旧宿主进程使用它；距离
最后一次从它派发 Worker 已超过该插件的 Worker 时限加 60 秒。Worker 运行不会超过时限，因此不需要跟踪每个 Worker 的
结束。

Server 启动时，在第一次扫描生成当前快照之后，删除 `plugin-versions/` 下所有其他快照和残留的临时目录。多个 Server 进程
共用一个数据目录时（同机多实例），每个进程只删除自己创建且不再使用的快照；启动清理只删除修改时间早于本进程启动时间的
快照。

---

## 4. 切换

### 4.1 状态

已激活插件记录（RFC #2549 设计 §4.2）增加：

```python
    version_digest: str | None         # 当前快照的摘要
    snapshot: Path | None              # 当前快照目录
    activated_at: datetime | None
    last_check_failure: str | None     # 最近一次检查失败的分类；切换成功后清空
    state: Literal["loaded", "failed"]
```

### 4.2 新增

检查通过后执行 RFC #2549 设计 §4.4 的注册，并注册插件路由（§5）和页面（§7）。检查失败时 `state="failed"`。

### 4.3 更新

| 情况 | 处理 |
| --- | --- |
| 检查通过，声明摘要不变 | 更新 `version_digest` 与 `snapshot`；替换宿主进程（§4.5） |
| 检查通过，声明摘要改变 | 计算新旧声明的差异：注销移除的 Source 类型、路由、页面，注册新增的；family 改名按移除旧 family、新增新 family 处理；用 `replace_binding` 更新 binding 的调度参数；更新快照；替换宿主进程 |
| 检查失败 | 只记录 `last_check_failure` 和 `check_failed` 事件，当前快照与宿主进程不变 |

### 4.4 移除

注销 Source 类型、family、合并注册、路由、页面和 binding（`replace_binding(name, None)`）；停止接收该插件的新同步调用；等待
进行中的同步调用结束或超时后停止宿主进程；快照按 §3.4 的规则在之后的扫描中删除。

### 4.5 宿主进程替换

```text
  -> 从新快照启动新宿主进程，等待 ready（超时按启动失败处理）
  -> 启动失败：停止新进程，保留旧进程，记录 check_failed
  -> 就绪：PluginHostHandle 原子替换为新进程；新请求发往新进程
  -> 旧进程标记为 draining：不再接收请求；进行中的请求完成后发送 shutdown；
     任一请求超过其时限时终止旧进程
  -> 旧进程退出后，旧快照按 §3.4 的规则在之后的扫描中删除
```

宿主进程处于重启退避、当前没有运行时，只更新快照，退避结束后直接从新快照启动。

### 4.6 多实例

- 各实例独立扫描，`/v1/plugins` 返回本实例的视图，包括 `version_digest`、`declaration_digest`、`state`、`activated_at` 和
  `last_check_failure`。
- binding 只由领导者注册；领导权转移时新领导者按本实例的记录注册。

### 4.7 数据升级

```text
宿主进程启动（新快照）
  -> 导入插件，核对声明，打开数据库、授权服务、专属存储和模型
  -> 声明了 upgrade：以插件自身身份执行 upgrade(ctx)，受 upgrade_timeout_seconds 约束
       -> 异常或超时：发送 HostFailed(category="upgrade_failed")，进程退出
  -> 执行 setup(ctx)，发送 HostReady
```

- `upgrade` 期间 `ctx` 的请求辅助方法处于"插件身份"模式：`reads`、`writer` 不检查调用者，不要求先 `require`；`caller`、
  `require`、`sources`、`scopes` 不可用。`setup` 之后恢复为按请求调用者执行。
- Server 收到 `upgrade_failed` 时按检查失败处理（§4.3），记录 `upgrade_failed` 事件；不进入宿主进程的重启退避，下一个版本
  被扫描到时再尝试。
- 声明了 `upgrade` 的插件在加载时立即启动宿主进程。Server 启动时，这类插件的 binding 在宿主进程就绪后才注册；热加载时，
  §4.5 中新宿主进程就绪后才切换快照，后台调用自然从切换后开始使用新快照。
- 宿主进程因崩溃而重启时会再次执行 `upgrade`，与"每次加载都调用"的约定一致，插件本来就要保证可重复执行。
- `background` 角色的实例不运行宿主进程：加载新版本时以 spawn 启动一个只执行 `upgrade` 的子进程（同样以插件身份、受
  `upgrade_timeout_seconds` 约束），成功后才把 binding 的 launcher 切到新快照；失败时按检查失败处理，继续用旧快照派发。
  每个实例都执行自己的 `upgrade`，新版本的 `generate` 只在本实例的 `upgrade` 完成后运行。
- `upgrade` 只做快速、可重复的变更；大量数据的格式变化由插件在读取时惰性迁移，或由部署者在升级前执行插件提供的迁移
  脚本，core 不提供额外机制。`upgrade` 中的写入器遵循 Worker 的写事务规则，一次最多开启一个写事务块。

---

## 5. 插件路由

### 5.1 挂载

Server 为插件分组注册一条通配路由：

```text
/v1/plugins/{name}/api/{path:path}     （GET、POST、PUT、PATCH、DELETE）
```

处理流程：

1. 认证调用者，未认证返回 401；
2. 按 `name` 找到加载成功的插件，不存在返回 404；
3. 在该插件的路由中按方法和路径匹配。每条路由的路径用 Starlette 的 `compile_path` 编译，匹配顺序为：没有参数的路径优先，
   其次按声明顺序；没有匹配时返回 404；
4. 读取请求体：上限为路由的 `max_body_bytes`，未声明时 `json` 为 1 MiB、`binary` 为部署上限；任何路由都不超过
   `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES`（默认 32 MiB）。超出返回 413；
5. 构造 `HostRequest(method="api", ...)`，携带调用者、路由模板、路径参数、查询参数和请求体，交给宿主进程；
6. 宿主进程：`access` 为 Scope 权限时，取 `scope_param` 对应的路径参数作为 Scope ID，按 `scope.read` 或
   `scope.contribute` 授权，不满足返回 403；`authenticated` 不再检查；校验 `request_model`；调用处理函数；
7. Server 映射响应（§5.3）。

Server 只做不需要插件、也不需要权限服务的部分：找到插件和路由、认证、限制请求大小。授权和之后的步骤都在宿主进程中完成，
与 RFC #2549 设计 §7 的分工一致。

### 5.2 二进制请求体与响应

二进制请求体以 bytes 直接放在 `HostRequest` 中，经 `Pipe` 传给宿主进程；处理函数返回的 `BinaryResponse` 同样以 bytes
放在 `HostResponse` 中传回。传递时 Server 与宿主进程各持有一份副本，单个请求的峰值内存约为上限的两倍。

### 5.3 响应映射

| 宿主进程结果 | HTTP |
| --- | --- |
| 返回 Pydantic 模型或 JSON 值 | 200，JSON |
| 返回 `BinaryResponse` | 200，按 `content_type` 与 `filename` 设置响应头 |
| 返回 `None` | 204 |
| `ApiError(status, code, message)` | `status`，错误码与消息取自 `ApiError`；`ApiError` 构造时只接受 401、403 以外的 4xx，否则构造本身抛出异常，按 500 处理 |
| `ctx.require` 或写入授权失败 | 403 |
| `request_model` 校验失败或 `InvalidContent` | 422 |
| 其他异常 | 500，只记录异常类型 |
| 超过 `timeout_seconds` | 504；只取消该请求（见 RFC #2549 设计 §7.3） |
| 宿主进程不可用、退避中，或因失去响应被终止时的其他请求 | 503 |

错误响应使用 Server 的统一错误格式。

### 5.4 OpenAPI

`GET /v1/plugins/{name}/openapi.json` 由路由表生成：每条路由一个 operation，`operationId` 为 `<name>__<handler>`；
`request_model`、`response_model` 的 JSON Schema 来自检查子进程导出的声明；`binary` 请求体为
`application/octet-stream`；统一加入 401、403、404、413、422、500、503、504 的错误响应；扩展字段
`x-powercontext-access` 记录 `access` 与 `scope_param`。

### 5.5 MCP 工具

- 现有的 `create_mcp_server` 在启动时从 Server 的 OpenAPI 文档生成工具，经内部桥接（`_InternalBridgeTransport`）调用
  HTTP 路由。插件工具复用同一桥接：每个 `mcp=True` 的路由生成一个工具，调用时以 MCP 会话的认证信息请求
  `/v1/plugins/{name}/api{path}`，认证、授权和错误处理都走 HTTP 路由本身。
- 输入 schema：路径参数和查询参数为字符串属性，`request_model` 的 JSON Schema 作为 `body` 属性；输出 schema 为
  `response_model` 的 JSON Schema。
- 注解：`GET` 且 `access` 为 `scope.read` 或 `authenticated` 时 `readOnlyHint=True`、`idempotentHint=True`；其他路由
  `readOnlyHint=False`。`openWorldHint` 均为 `False`。
- 热加载：插件工具由一个 FastMCP `Provider` 提供，每次 `tools/list` 和调用时从当前已加载的插件生成，切换后立即反映。
  Server 的 MCP 端点是无状态 HTTP（`stateless_http=True`），没有可以推送工具列表变化通知的会话，客户端在下一次列出
  工具时看到变化。
- 工具名为 `<插件名>__<handler>`，插件名原样保留。插件名不含下划线，第一个双下划线之前就是插件名，不同插件的工具名不会
  相撞；超过 64 个字符或与内置工具重名时，该插件检查失败。

---

## 6. 处理函数的能力

处理函数在宿主进程中运行。宿主进程是"core 运行时 + 插件"（RFC #2549 设计 §7.1），已经有数据库连接、按 Worker 方式重建的
权限服务（`AccessControlService`）和 Scope 服务，因此 `ctx` 上的能力都在宿主进程本地实现，以请求携带的调用者授权：

| 能力 | 实现 |
| --- | --- |
| `ctx.require(action, scope_id)` | 用本地权限服务检查调用者是否具备该权限 |
| `ctx.allowed(action, scope_id)` | 同上，返回布尔值而不抛出异常 |
| `ctx.caller` | 当前请求的调用者 |
| `ctx.reads(scope_id)` | 先检查 `scope.read`，再创建只读视图，读取按调用者身份过滤 |
| `ctx.writer(scope_id)` | 以调用者身份创建写入器（RFC #2549 设计 §6.5 的授权方式），写操作按调用者的 `scope.contribute` 授权，不限定写操作种类 |
| `ctx.sources.submit(scope_id, definition, value)` | 检查调用者的 `scope.contribute`；执行 `resolve` 与 Projection，以调用者身份保存 |
| `ctx.scopes.create(...)` | 检查调用者具备 `server.admin`；创建 Scope 并加入外部引用 `{"kind": "powercontext.plugin", "value": <插件名>}`；成功后让调用者成为该 Scope 的管理员，与 `POST /v1/scopes` 的实现相同 |
| `ctx.scopes.update(...)` | 检查调用者具备 `server.admin`，确认 Scope 带有本插件的外部引用，再按 `PUT /v1/scopes/{scope_id}` 的实现执行 |
| `ctx.scopes.list()` | 列出调用者可见、带有本插件外部引用的 Scope |

---

## 7. 插件页面

### 7.1 路由

| 路由 | 认证 | 说明 |
| --- | --- | --- |
| `GET /dashboard/plugins/{name}` | Dashboard 会话 | 宿主页面，`?scope=` 指定 Scope |
| `GET /dashboard/plugins/{name}/ui/{path:path}` | 不要求 | 从插件的 `directory` 提供静态文件（热加载实现后为当前快照） |
| `* /dashboard/plugins/{name}/api/{path:path}` | Dashboard 会话 + 同源 | 服务端转发到 `/v1/plugins/{name}/api/{path}` |
| `GET /dashboard/plugins/_client.js` | 不要求 | 插件页面客户端脚本 |

### 7.2 静态文件

- 解析路径后确认位于 `directory` 内，拒绝 `..` 与指向目录外的符号链接；
- 扩展名白名单：`.html`、`.js`、`.css`、`.json`、`.svg`、`.png`、`.jpg`、`.webp`、`.woff2`；单个文件不超过 5 MiB；
- 响应头（`<own>` 为 `/dashboard/plugins/{name}/ui/` 的绝对地址，`<client>` 为 `_client.js` 的绝对地址）：

  ```text
  Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads;
                           default-src 'none'; script-src <own> <client>; style-src <own> 'unsafe-inline';
                           img-src <own> data: https:; font-src <own>; connect-src https:; form-action https:;
                           base-uri 'none'; frame-ancestors 'self'
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Cache-Control: no-cache
  ```

  `sandbox` 使页面运行在不透明来源，`'self'` 无法匹配插件自己的地址，因此脚本、样式、图片和字体都写成绝对地址。

### 7.3 宿主页面脚本

```text
渲染 <iframe sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads">
先注册监听，再设置 src="/dashboard/plugins/{name}/ui/{entry}"，保证第一次 load 就是插件页面
loads = 0
iframe.onload:
  loads += 1
  loads == 1：发送 {type: "powercontext.context", context}
  loads > 1：停止转发，显示提示，由用户重新打开插件页面
window.onmessage(event):
  未在转发或 event.source !== iframe.contentWindow：忽略
  data.type == "powercontext.ready"：再次发送 context
  data.type == "powercontext.request"：
    方法和路径匹配本插件声明的路由（路由表随页面下发），且路径不含 . 或 .. 段
      → fetch("/dashboard/plugins/{name}/api" + path, {method, body, credentials: "same-origin"})
    否则回复 {status: 404, error: "route_not_declared"}
    回复 {type: "powercontext.response", id, status, contentType, filename, json | blob}；网络错误时 {status: 0, error: "network_error"}
```

### 7.4 消息格式

| 方向 | 消息 |
| --- | --- |
| 宿主页面 → 插件页面 | `{type: "powercontext.context", context: {scopeId, locale, theme}}` |
| 插件页面 → 宿主页面 | `{type: "powercontext.ready"}`：客户端脚本加载后发送，宿主页面回复 context |
| 插件页面 → 宿主页面 | `{type: "powercontext.request", id, method, path, query, json?, body?, contentType?}` |
| 宿主页面 → 插件页面 | `{type: "powercontext.response", id, status, contentType, filename?, json? \| blob?, error?}` |

`_client.js` 定义 `window.powercontextPluginClient()`，返回 `context(): Promise<Context>` 和
`request(method, path, {query, json, body, contentType}): Promise<{status, ok, contentType, filename, json, blob}>`；
内部按 `id` 匹配响应，只接受来自父窗口的消息，`error` 时以带 `status` 的异常拒绝。客户端不设超时，时限由服务端路由决定。

### 7.5 服务端转发

`/dashboard/plugins/{name}/api/...` 要求 Dashboard 会话；要求 `Sec-Fetch-Site: same-origin`，缺失时校验 `Origin` 与
Server 同源；`Origin: null` 一律拒绝，返回 403。插件未加载或未声明页面时返回 404。通过后以会话身份执行与
`/v1/plugins/{name}/api/...` 相同的处理（§5.1），返回状态码、`Content-Type`、`Content-Disposition` 和响应体。

---

## 8. 可观测性

已实现：插件接口请求的审计操作名为 `plugin:<插件名>:<handler>`。以下字段和指标随热加载一起实现：

| 字段 | 含义 |
| --- | --- |
| `plugin_event` | `loaded`、`updated`、`removed`、`check_failed` |
| `version_digest` | 本次生效的快照摘要 |
| `declaration_digest` | 本次生效的声明摘要 |
| `plugin_route` | 自定义接口的路由模板，不含实际参数值 |
| `plugin_method` | 增加 `api` |
| `scan_ms` | 一次扫描的耗时，毫秒 |

指标：

- `powercontext_plugin_scan_duration_seconds`（直方图）；
- `powercontext_plugin_api_requests_total`，标签 `plugin_name`、`plugin_route`、状态类别（2xx、4xx、5xx）；
- `powercontext_plugin_api_duration_seconds`（直方图），标签同上。

---

## 9. 默认值汇总

| 项 | 默认值 | 配置 |
| --- | --- | --- |
| 扫描间隔 | 60 秒 | `POWERCONTEXT_SERVER_PLUGINS_RESCAN_SECONDS`（0 关闭热加载） |
| 稳定判断 | 连续 2 轮摘要相同 | 固定 |
| 宿主进程就绪超时 | 60 秒 | 固定 |
| 路由默认时限 | 30 秒 | 声明 `timeout_seconds` |
| JSON 请求体上限 | 1 MiB | 路由 `max_body_bytes` |
| 二进制请求体上限 | 32 MiB | 路由 `max_body_bytes`，不超过 `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` |
| 每个插件的路由数 | 至多 200 | 固定 |
| 页面静态文件上限 | 5 MiB | 固定 |
| 数据升级时限 | 600 秒 | 声明 `upgrade_timeout_seconds` |

---

## 10. 任务拆分

| # | 任务 | 依赖 | 状态 |
| --- | --- | --- | --- |
| T1 | 扫描循环、版本摘要、稳定判断 | RFC #2549 实现（#2616） | #2638 已完成 |
| T2 | 版本快照：生成、只读、保留与清理；检查子进程、宿主进程、Worker 改为从快照导入 | T1 | #2638 已完成 |
| T3 | 切换：新增、更新、移除；声明差异的注册与注销；宿主进程替换 | T2 | #2638 已完成 |
| T4 | 插件路由：声明、匹配、授权、请求体、响应映射、二进制文件交换 | RFC #2549 实现（#2616） | #2616 已完成 |
| T5 | 处理函数能力：`require`、`reads`、`writer`、`sources.submit`、`scopes`，在宿主进程中本地实现 | T4 | #2616 已完成 |
| T6 | 插件 OpenAPI 生成 | T4 | #2638 已完成 |
| T7 | 插件页面：静态文件、宿主页面、转发、`_client.js` | T4 | #2616 已完成 |
| T8 | `/v1/plugins` 响应字段与可观测性 | T3、T4 | #2638 已完成 |
| T9 | 浏览器验证：CSP `sandbox` 下 `'self'` 的匹配、不透明来源请求的 cookie 行为，在 Chromium、Firefox、Safari 上执行 | T7 | Chromium 已手动验证；Firefox、Safari 待验证 |
| T10 | MCP 工具：生成、注解、热加载时增删与通知 | T3、T4 | #2638 已完成 |
| T11 | 数据升级：宿主进程启动时执行 `upgrade`、插件身份上下文、`upgrade_failed` 的处理、binding 等待就绪 | T3、T5 | #2638 已完成 |

---

## 11. 待明确事项

| 事项 | 说明 |
| --- | --- |
| 同机多实例共用数据目录 | 快照清理按"本进程创建"区分的做法是否足够，还是要求每个实例使用独立的数据目录 |
| 大请求体的流式传输 | 请求体完整读入后以 bytes 经 `Pipe` 转交；是否需要直接流式转交宿主进程 |
| 浏览器兼容性 | CSP `sandbox` 下的页面行为已用 Playwright 在 Chromium 上手动验证；Firefox、Safari 待验证（T9） |

---

## 12. RFC 之外的设计决定

RFC 只规定契约。下表列出本文为实现契约所做、RFC 没有规定的决定，评审时逐项确认。宿主进程的进程模型与通信方式见
RFC #2549 设计 §16 的 D1、D2。

| # | 决定 | 理由 | 状态 |
| --- | --- | --- | --- |
| E1 | 版本快照放在数据目录下，以内容摘要命名，设为只读（§3） | 与 Dify 按版本解压插件包的做法一致；只读保证快照不被修改 | 已采用 |
| E2 | 同机多个实例共用一个数据目录时，每个进程只清理自己创建且不再使用的快照（§3.4） | 防止删除其他实例正在使用的快照 | 待确认：也可以要求每个实例使用独立的数据目录 |
| E3 | 插件接口的二进制请求体与响应以 bytes 直接经 `Pipe` 传递，受同一上限约束（§5.2） | 随 RFC #2549 设计 D1；不需要临时文件及其清理。代价是单个请求的峰值内存约为上限的两倍 | 已采用 |
| E4 | 插件接口的授权和 `ctx` 上的全部能力在宿主进程本地实现，Server 只找路由、认证和限制请求大小（§5.1、§6） | 随 RFC #2549 设计 D2；不需要回调通道 | 已采用 |
| E5 | 路由匹配时无参数的路径优先，其次按声明顺序（§5.1） | 与常见 Web 框架一致，避免参数路径吞掉固定路径 | 已采用 |
| E6 | OpenAPI 的 `operationId` 与 MCP 工具名都是 `<插件名>__<handler>`（§5.4、§5.5） | 稳定、可预测，便于对照；插件名不含下划线，以双下划线分隔不会撞名 | 已采用 |
| E7 | MCP 工具复用现有的内部 HTTP 桥接，鉴权走 HTTP 路由本身（§5.5） | 与内置工具一致，不另写一套鉴权 | 已采用 |
| E8 | 插件页面跳转或刷新后停止转发并提示用户重新打开（§7.3） | 页面跳转后无法区分插件页面与外部页面 | 已采用 |
| E9 | 静态文件设 `no-cache`（§7.2） | 插件更新后浏览器立即取到新文件；热加载实现后可加上以版本摘要为前缀的 ETag | 已采用 |
| E10 | 接收请求的实例在新宿主进程中、`setup` 之前执行 `upgrade`；`background` 实例用只执行 `upgrade` 的短时子进程（§4.7） | 宿主进程本来就要在使用新版本前启动，复用它的运行时和资源；`background` 实例没有宿主进程，又要保证新 `generate` 运行在升级后的数据上。执行失败沿用保留旧版本的流程 | 待确认 |
| E11 | `upgrade` 期间 `reads`、`writer` 以插件身份执行，不提供调用者相关的能力（§4.7） | 升级没有请求调用者，与 Worker 的权限模型一致 | 待确认 |
| E12 | 宿主进程崩溃重启时再次执行 `upgrade`（§4.7） | 与"每次加载都调用、必须可重复执行"的约定一致，不需要额外记录"本进程是否已升级" | 待确认 |
