# 系统设计：插件热加载与自定义接口（Plugin Reload and APIs）

| 项 | 内容 |
| --- | --- |
| 设计源 | [RFC #2579 插件热加载与自定义接口](https://github.com/oceanbase/powercontext/pull/2579) |
| 前置设计 | [插件接口设计](2549_plugin_interfaces_design.md)（RFC #2549） |
| 文档状态 | 设计草案 |
| 日期 | 2026-11-11 |
| 技术栈 | Python 3.11+ / pydantic v2 / FastAPI / Starlette；Dashboard 为服务端渲染页面加少量脚本 |

本文是 RFC #2579 的实现设计，建立在 RFC #2549 的实现设计之上：宿主进程、已激活插件记录、写入器和用户修改模式都沿用
该文档，本文只描述新增和改变的部分。两者冲突时以 RFC 为准，并修订本文。

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
| `src/powercontext/server/plugin_reload.py` | 新增 | 扫描循环、摘要计算、稳定判断、快照生成与清理 |
| `src/powercontext/server/plugins.py` | 修改 | `PluginManager` 增加新增、更新、移除的切换逻辑；已激活插件记录增加版本信息 |
| `src/powercontext/server/plugin_hosts.py` | 修改 | 宿主进程替换：新进程就绪后切换，旧进程排空后停止 |
| `src/powercontext/server/plugin_api.py` | 新增 | 插件路由分组、路由匹配、授权、请求体读取、转交宿主进程、响应映射、OpenAPI 生成 |
| `src/powercontext/builtin/plugins/host.py` | 修改 | 处理 `api` 请求：Scope 授权、`request_model` 校验、调用处理函数；在本地提供 `ctx.require`、`ctx.reads`、`ctx.writer`、`ctx.sources`、`ctx.scopes` |
| `src/powercontext/builtin/plugins/host_protocol.py` | 修改 | 增加 `api` 请求与响应 |
| `src/powercontext/plugins.py` | 修改 | `PluginApi`、`Route`、`ApiRequest`、`BinaryResponse`、`ApiError`、`Caller`、`PluginPages` |
| `src/powercontext/server/dashboard/plugins.py` | 新增 | 插件页面的宿主页面、静态文件、服务端转发、`_client.js` |
| `src/powercontext/server/dashboard/static/plugin-host.js` | 新增 | 宿主页面脚本：iframe 管理与消息转发 |
| `src/powercontext/server/dashboard/static/plugin-client.js` | 新增 | 提供给插件页面的客户端脚本 |
| `openapi/powercontext.yaml` | 修改 | `GET /v1/plugins/{name}/openapi.json`；`/v1/plugins` 响应字段 |

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
3. 把文件设为只读（文件 `0444`，目录 `0555`）；
4. 改名为 `<版本摘要>/`；目标已存在时删除临时目录，直接使用已有快照。

### 3.3 使用

- 检查子进程、宿主进程、Worker 都以快照目录为插件目录：快照目录加入 `sys.path`，`ctx.plugin_dir` 指向快照。
- `PluginWorkerSpec` 增加 `snapshot` 字段；Supervisor binding 的 launcher 在派发时读取当前快照，因此切换版本后新派发的
  Worker 自动使用新快照，不需要重新注册 binding。
- 使用快照时不再核对代码指纹：快照只读，且路径中的摘要就是其内容。

### 3.4 引用与清理

`PluginManager` 为每个快照维护进程内引用计数：当前版本计 1；每个运行中的宿主进程计 1；每个已派发、尚未完成的 Worker
计 1。扫描末尾删除引用计数为 0 的快照。

Server 启动时，在第一次扫描生成当前快照之后，删除 `plugin-versions/` 下所有其他快照和残留的临时目录。多个 Server 进程
共用一个数据目录时（同机多实例），每个进程只删除自己创建且不再使用的快照；启动清理只删除修改时间早于本进程启动时间的
快照。

---

## 4. 切换

### 4.1 状态

已激活插件记录（RFC #2549 设计 §3.2）增加：

```python
    version_digest: str | None         # 当前快照的摘要
    snapshot: Path | None              # 当前快照目录
    activated_at: datetime | None
    last_check_failure: str | None     # 最近一次检查失败的分类；切换成功后清空
    state: Literal["loaded", "failed"]
```

### 4.2 新增

检查通过后执行 RFC #2549 设计 §3.4 的注册，并注册插件路由（§5）和页面（§7）。检查失败时 `state="failed"`。

### 4.3 更新

| 情况 | 处理 |
| --- | --- |
| 检查通过，声明摘要不变 | 更新 `version_digest` 与 `snapshot`；替换宿主进程（§4.5） |
| 检查通过，声明摘要改变 | 计算新旧声明的差异：注销移除的 Source 类型、路由、页面，注册新增的；family 改名按移除旧 family、新增新 family 处理；用 `replace_binding` 更新 binding 的调度参数；更新快照；替换宿主进程 |
| 检查失败 | 只记录 `last_check_failure` 和 `check_failed` 事件，当前快照与宿主进程不变 |

### 4.4 移除

注销 Source 类型、family、合并注册、路由、页面和 binding（`replace_binding(name, None)`）；停止接收该插件的新同步调用；等待
进行中的同步调用结束或超时后停止宿主进程；当前快照的引用计数减 1。

### 4.5 宿主进程替换

```text
  -> 从新快照启动新宿主进程，等待 ready（超时按启动失败处理）
  -> 启动失败：停止新进程，保留旧进程，记录 check_failed
  -> 就绪：PluginHostHandle 原子替换为新进程；新请求发往新进程
  -> 旧进程标记为 draining：不再接收请求；进行中的请求完成后发送 shutdown；
     任一请求超过其时限时终止旧进程
  -> 旧进程退出后，旧快照引用计数减 1
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
  -> 执行 create(ctx)，发送 HostReady
```

- `upgrade` 期间 `ctx` 的请求辅助方法处于"插件身份"模式：`reads`、`writer` 不检查调用者，不要求先 `require`；`caller`、
  `require`、`sources`、`scopes` 不可用。`create` 之后恢复为按请求调用者执行。
- Server 收到 `upgrade_failed` 时按检查失败处理（§4.3），记录 `upgrade_failed` 事件；不进入宿主进程的重启退避，下一个版本
  被扫描到时再尝试。
- 声明了 `upgrade` 的插件在加载时立即启动宿主进程。Server 启动时，这类插件的 binding 在宿主进程就绪后才注册；热加载时，
  §4.5 中新宿主进程就绪后才切换快照，后台调用自然从切换后开始使用新快照。
- 宿主进程因崩溃而重启时会再次执行 `upgrade`，与"每次加载都调用"的约定一致，插件本来就要保证可重复执行。

---

## 5. 插件路由

### 5.1 挂载

Server 为插件分组注册一条通配路由：

```text
/v1/plugins/{name}/api/{path:path}     （GET、POST、PUT、PATCH、DELETE）
```

处理流程：

1. 按 `name` 找到已激活插件，不存在或 `state != "loaded"` 返回 404；
2. 在该插件的路由表中按 `(method, path)` 匹配。路由表由声明编译为 Starlette 的 `compile_path` 正则，匹配顺序为：没有参数的
   路径优先，其次按声明顺序；
3. 认证调用者，未认证返回 401；
4. 读取请求体：`json` 不超过 1 MiB，`binary` 不超过 `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES`，超出返回 413；
5. 构造 `HostRequest(method="api", ...)`，携带调用者、路由模板、路径参数、查询参数和请求体，交给宿主进程；
6. 宿主进程：`access` 为 Scope 权限时，取 `scope_param` 对应的路径参数作为 Scope ID，按 `scope.read` 或
   `scope.contribute` 授权，不满足返回 403；`authenticated` 不再检查；校验 `request_model`；调用处理函数；
7. Server 映射响应（§5.3）。

Server 只做不需要插件、也不需要权限服务的部分：找到插件和路由、认证、限制请求大小。授权和之后的步骤都在宿主进程中完成，
与 RFC #2549 设计 §6 的分工一致。

### 5.2 二进制请求体与响应

二进制请求体以 bytes 直接放在 `HostRequest` 中，经 `Pipe` 传给宿主进程；处理函数返回的 `BinaryResponse` 同样以 bytes
放在 `HostResponse` 中传回。请求和响应都受 `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` 限制（默认 32 MiB），超过上限
的响应按 500 处理并记录。传递时 Server 与宿主进程各持有一份副本，单个请求的峰值内存约为上限的两倍。

### 5.3 响应映射

| 宿主进程结果 | HTTP |
| --- | --- |
| 返回 `response_model` 实例 | 200，JSON |
| 返回 `BinaryResponse` | 200，按 `content_type` 与 `filename` 设置响应头 |
| 返回 `None` | 204 |
| `ApiError(status, code, message)` | `status`，body 为 `{"code", "message"}`；`status` 不在 400–499 或为 401、403 时按 500 处理 |
| `ctx.require` 或写入授权失败 | 403 |
| `request_model` 校验失败 | 422 |
| 其他异常 | 500，只记录异常类型 |
| 超过 `timeout_seconds` | 504 |
| 宿主进程不可用或退避中 | 503 |

### 5.4 OpenAPI

`GET /v1/plugins/{name}/openapi.json` 由路由表生成：每条路由一个 operation，`operationId` 为 `<name>_<handler>`；
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
- 热加载：切换完成后调用 FastMCP 3.4 的 `add_tool` / `remove_tool`，`tools/list` 总是返回当前工具。FastMCP 的工具列表
  变化通知只能在某个会话的请求上下文中发送，热加载发生在请求之外；实现时在会话的下一次请求中补发通知，做不到时客户端
  在下一次列出工具时看到变化。
- 工具名与内置工具或其他插件的工具重名时，该插件检查失败。

---

## 6. 处理函数的能力

处理函数在宿主进程中运行。宿主进程是"core 运行时 + 插件"（RFC #2549 设计 §6.1），已经有数据库连接、按 Worker 方式重建的
权限服务（`AccessControlService`）和 Scope 服务，因此 `ctx` 上的能力都在宿主进程本地实现，以请求携带的调用者授权：

| 能力 | 实现 |
| --- | --- |
| `ctx.require(action, scope_id)` | 用本地权限服务检查调用者是否具备该权限 |
| `ctx.reads(scope_id)` | 先检查 `scope.read`，再创建只读视图，读取按调用者身份过滤 |
| `ctx.writer(scope_id)` | 只接受 `request.scope_id` 且路由为 `scope.contribute`；以调用者身份创建写入器（RFC #2549 设计 §5.5 的授权方式），不限定写操作种类 |
| `ctx.sources.submit(scope_id, definition, value)` | 同上的范围限制；执行 `resolve` 与 Projection，以调用者身份保存 |
| `ctx.scopes.create(...)` | 检查调用者具备 `server.admin`；调用 `ScopeApplication.create`，加入外部引用 `{"kind": "powercontext.plugin", "value": <插件名>}`；成功后调用 `AccessControlService.bootstrap_static_scope` 让调用者成为管理员，与 `POST /v1/scopes` 的实现相同 |
| `ctx.scopes.update(...)` | 先确认 Scope 带有本插件的外部引用，再按 `PUT /v1/scopes/{scope_id}` 的实现与授权执行 |
| `ctx.scopes.list()` | 列出调用者可见、带有本插件外部引用的 Scope |

---

## 7. 插件页面

### 7.1 路由

| 路由 | 认证 | 说明 |
| --- | --- | --- |
| `GET /dashboard/plugins/{name}` | Dashboard 会话 | 宿主页面，`?scope=` 指定 Scope |
| `GET /dashboard/plugins/{name}/ui/{path:path}` | 不要求 | 从当前快照的 `directory` 提供静态文件 |
| `* /dashboard/plugins/{name}/api/{path:path}` | Dashboard 会话 + 同源 | 服务端转发到 `/v1/plugins/{name}/api/{path}` |
| `GET /dashboard/plugins/_client.js` | 不要求 | 插件页面客户端脚本 |

### 7.2 静态文件

- 解析路径后确认位于快照的 `directory` 内，拒绝 `..` 与指向目录外的符号链接；
- 扩展名白名单：`.html`、`.js`、`.css`、`.json`、`.svg`、`.png`、`.jpg`、`.webp`、`.woff2`；单个文件不超过 5 MiB；
- 响应头：

  ```text
  Content-Security-Policy: sandbox allow-scripts allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox;
                           default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
                           img-src 'self' data: https:; font-src 'self'; connect-src https:; frame-ancestors 'self'
  X-Content-Type-Options: nosniff
  Cache-Control: no-cache
  ETag: "<版本摘要>:<相对路径>"
  ```

### 7.3 宿主页面脚本

```text
渲染 <iframe sandbox="allow-scripts allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox"
            src="/dashboard/plugins/{name}/ui/{entry}">
loads = 0
iframe.onload:
  loads += 1
  loads == 1：postMessage(iframe.contentWindow, {type: "powercontext.context", scopeId, locale, theme}, "*")
  loads > 1：停止转发，显示"重新打开插件页面"；点击后移除并重建 iframe，loads 归零
window.onmessage(event):
  event.source !== iframe.contentWindow 或 loads != 1：忽略
  data.type == "powercontext.request"：
    path 匹配本插件声明的路由（路由表随页面下发）→ fetch("/dashboard/plugins/{name}/api" + path, {method, body})
    否则回复 status 404
    回复 {type: "powercontext.response", id, status, headers, body}；二进制响应以 Blob 传回
```

### 7.4 消息格式

| 方向 | 消息 |
| --- | --- |
| 宿主页面 → 插件页面 | `{type: "powercontext.context", scopeId: string, locale: "zh" \| "en", theme: "light" \| "dark"}` |
| 插件页面 → 宿主页面 | `{type: "powercontext.request", id: string, method: string, path: string, query?: Record<string, string[]>, body?: unknown \| Blob}` |
| 宿主页面 → 插件页面 | `{type: "powercontext.response", id: string, status: number, headers: Record<string, string>, body: unknown \| Blob}` |

`_client.js` 导出 `powercontextPluginClient()`，提供 `context(): Promise<Context>` 和
`request(method, path, {query, body}): Promise<{status, headers, body}>`，内部按 `id` 匹配响应，默认 60 秒超时。

### 7.5 服务端转发

`/dashboard/plugins/{name}/api/...` 要求 Dashboard 会话 cookie；要求 `Sec-Fetch-Site: same-origin`，缺失时用
`dashboard/session.py` 的 `same_origin` 校验 `Origin`；`Origin: null` 一律拒绝。通过后以 `authentication_headers` 取得会话
身份，调用 `/v1/plugins/{name}/api/...`，原样返回状态码、`Content-Type`、`Content-Disposition` 和响应体。

---

## 8. 可观测性

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
| JSON 请求体上限 | 1 MiB | 固定 |
| 二进制请求体与响应上限 | 32 MiB | `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` |
| 页面静态文件上限 | 5 MiB | 固定 |
| `_client.js` 请求超时 | 60 秒 | 插件页面可传入 |
| 数据升级时限 | 600 秒 | 声明 `upgrade_timeout_seconds` |

---

## 10. 任务拆分

| # | 任务 | 依赖 |
| --- | --- | --- |
| T1 | 扫描循环、版本摘要、稳定判断 | RFC #2549 设计 T2 |
| T2 | 版本快照：生成、只读、引用计数、清理；检查子进程、宿主进程、Worker 改为从快照导入 | T1、RFC #2549 设计 T3 |
| T3 | 切换：新增、更新、移除；声明差异的注册与注销；宿主进程替换 | T2 |
| T4 | 插件路由：声明、匹配、授权、请求体、响应映射、二进制文件交换 | RFC #2549 设计 T3 |
| T5 | 处理函数能力：`require`、`reads`、`writer`、`sources.submit`、`scopes`，在宿主进程中本地实现 | T4 |
| T6 | 插件 OpenAPI 生成 | T4 |
| T7 | 插件页面：静态文件、宿主页面、转发、`_client.js` | T4 |
| T8 | `/v1/plugins` 响应字段与可观测性 | T3、T4 |
| T9 | 浏览器验证：CSP `sandbox` 下 `'self'` 的匹配、不透明来源请求的 cookie 行为，在 Chromium、Firefox、Safari 上执行 | T7 |
| T10 | MCP 工具：生成、注解、热加载时增删与通知 | T3、T4 |
| T11 | 数据升级：宿主进程启动时执行 `upgrade`、插件身份上下文、`upgrade_failed` 的处理、binding 等待就绪 | T3、T5 |

---

## 11. 待明确事项

| 事项 | 说明 |
| --- | --- |
| 同机多实例共用数据目录 | 快照清理按"本进程创建"区分的做法是否足够，还是要求每个实例使用独立的数据目录 |
| 大请求体的流式传输 | 第一版完整读入后经文件交换；是否需要直接流式转交宿主进程 |

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
| E6 | OpenAPI 的 `operationId` 与 MCP 工具名都是 `<插件名>_<handler>`（§5.4、§5.5） | 稳定、可预测，便于对照 | 已采用 |
| E7 | MCP 工具复用现有的内部 HTTP 桥接，鉴权走 HTTP 路由本身（§5.5） | 与内置工具一致，不另写一套鉴权 | 已采用 |
| E8 | 插件页面跳转或刷新后显示"重新打开插件页面"，重建 iframe 后恢复转发（§7.3） | 页面跳转后无法区分插件页面与外部页面 | 已采用 |
| E9 | 静态文件以 `<版本摘要>:<相对路径>` 作为 ETag 并设 `no-cache`（§7.2） | 切换版本后浏览器立即取到新文件 | 已采用 |
| E10 | `upgrade` 在新宿主进程中、`create` 之前执行，而不是另起一个子进程（§4.7） | 宿主进程本来就要在使用新版本前启动，复用它的运行时和资源；执行失败等同于新宿主进程启动失败，沿用现有的保留旧版本流程 | 待确认 |
| E11 | `upgrade` 期间 `reads`、`writer` 以插件身份执行，不提供调用者相关的能力（§4.7） | 升级没有请求调用者，与 Worker 的权限模型一致 | 待确认 |
| E12 | 宿主进程崩溃重启时再次执行 `upgrade`（§4.7） | 与"每次加载都调用、必须可重复执行"的约定一致，不需要额外记录"本进程是否已升级" | 待确认 |
