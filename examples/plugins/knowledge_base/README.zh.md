# 知识库（RAG）插件

[English](README.md)

一个完整的 PowerContext 插件示例：管理知识库、批量上传 Markdown、纯文本和 PDF 文档，选择解析方式后切块、建索引，
支持关键词和向量混合检索，并用 Server 配置的生成模型回答问题。它同时展示了插件的各项能力：生成 Artifact、自定义
Source 类型、插件接口、Dashboard 页面、专属存储和模型调用。插件开发的说明见
[编写插件](../../../docs/zh/docs/develop/plugins.md)。

## 部署

1. 把本目录复制到 Server 的插件目录，目录名就是插件名：

   ```bash
   cp -r examples/plugins/knowledge_base /srv/powercontext/plugins/knowledge-base
   ```

2. 在 Server 的 Python 环境中安装 PDF 解析依赖（不需要 PDF 时可以跳过，插件会只接受 Markdown 和纯文本）：

   ```bash
   uv pip install -r /srv/powercontext/plugins/knowledge-base/requirements.txt
   ```

3. 配置插件目录并启用 Dashboard，然后重启 Server：

   ```bash
   export POWERCONTEXT_SERVER_PLUGINS_DIR=/srv/powercontext/plugins
   export POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true
   ```

4. （可选）配置模型。没有 embedding 模型时，插件使用内置的哈希向量，只适合演示；没有生成模型时，问答只返回检索到的片段：

   ```bash
   export OPENAI_API_KEY=...
   export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:text-embedding-3-small
   export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=openai-3-small
   export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=1536
   export POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL=openai:gpt-4.1-mini
   ```

5. 用管理员凭据确认插件已加载：`GET /v1/plugins` 中 `knowledge-base` 的 `status` 为 `loaded`。

## 在 Dashboard 中使用

登录 Dashboard 后，左侧导航中的"知识库"就是插件页面（`/dashboard/plugins/knowledge-base`）：

- **新建知识库**：需要 `server.admin` 权限。每个知识库对应一个 Scope，Scope 对用户不可见。
- **上传**：拖拽或选择文件，支持 `.md`、`.markdown`、`.txt`、`.pdf`，单个文件不超过 32 MiB。
  - 知识库没有默认解析方式时，上传后停在"待配置"，可以撤销；批量上传完成后，在上方选择解析方式和参数，点"开始处理"。
  - 勾选"同时设为默认"后，之后的上传会直接处理。默认方式也可以在"编辑"中修改或取消。
- **解析方式**：按段落（相邻段落合并到指定长度）、按标题（每个 Markdown 标题一节）、固定长度。PDF 按页提取文字，
  每个切块记录页码；扫描件没有文字层，需要 OCR，本插件不支持。
- **切块**：打开文档的切块，可以修改、插入、删除后保存为新版本，也可以恢复上一版本，或换一种解析方式重新处理。
- **检索与问答**：在"检索与问答"中选择知识库，用关键词、向量或混合方式检索，或者提问，回答会标注引用的片段和页码。

## 通过接口使用

所有接口挂在 `/v1/plugins/knowledge-base/api` 下，使用与其他接口相同的凭据：

| 接口 | 说明 |
| --- | --- |
| `POST /knowledge-bases` | 新建知识库，`{"name", "description"}`，需要 `server.admin` |
| `GET /knowledge-bases`、`GET/PATCH/DELETE /knowledge-bases/{kb_id}` | 列出、查看、修改（包括 `default_parser`）、删除知识库 |
| `POST /knowledge-bases/{kb_id}/documents?name=<文件名>` | 上传文件，请求体是文件内容 |
| `POST /knowledge-bases/{kb_id}/processing` | 用指定解析方式处理待配置的文档，或用 `document_ids` 重新处理指定文档 |
| `GET /knowledge-bases/{kb_id}/documents`、`GET .../documents/{document_id}` | 文档列表和状态（`pending_config`、`processing`、`ready`、`failed`） |
| `GET .../documents/{document_id}/content` | 下载原文件 |
| `GET/PUT .../documents/{document_id}/chunks` | 查看（可带 `revision`）和编辑切块 |
| `POST .../documents/{document_id}/restorations` | 恢复到指定版本 |
| `DELETE .../documents/{document_id}` | 撤销未处理的上传，或删除文档 |
| `GET /parsers` | 可用的解析方式和参数 |
| `POST /search` | 跨知识库检索，`{"query", "knowledge_base_ids", "mode"}` |
| `POST /ask` | 跨知识库问答，`{"question", "knowledge_base_ids"}` |

```bash
API=http://127.0.0.1:8000/v1/plugins/knowledge-base/api
KB=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  $API/knowledge-bases -d '{"name": "运维手册"}' | jq -r .id)
curl -X POST -H "Authorization: Bearer $TOKEN" --data-binary @tls.md "$API/knowledge-bases/$KB/documents?name=tls.md"
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  $API/knowledge-bases/$KB/processing -d '{"parser": {"name": "paragraph"}, "make_default": true}'
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  $API/ask -d "{\"question\": \"证书快过期了怎么办？\", \"knowledge_base_ids\": [\"$KB\"]}"
```

## 实现方式

| 文件 | 内容 |
| --- | --- |
| `plugin.py` | 插件声明、两个 Source 定义、接口处理函数、后台生成 |
| `kb_store.py` | 插件专属存储中的表：知识库与 Scope 的映射、原文件、上传与处理记录、按内容寻址的切块组和向量 |
| `kb_parsers.py` | 解析器注册表与 PDF 文字提取 |
| `kb_text.py` | 分词、BM25、哈希向量和排序融合 |
| `ui/` | Dashboard 页面 |

- **知识库就是 Scope**：新建知识库时插件调用 `ctx.scopes.create` 创建 Scope，并在自己的存储中记录映射；接口只暴露知识库 ID。
- **Source 记录用户的要求**：确认解析方式或重新处理时提交 `kb-document` Source（文件摘要和解析方式），编辑切块时提交
  `kb-chunk-edit` Source（编辑人、基于的版本和保存的切块组）。文件和切块本身在插件存储中，Source 只记录摘要。
- **生成写出文档版本**：`generate` 解析文件、生成向量，然后在一个写事务中写入 `document` Artifact 的新版本，并引用对应的
  Source 作为出处。编辑只在文档仍处于被编辑的版本时生效。
- **检索在插件中完成**：`document` 的内容只记录切块组的摘要，检索时插件读取当前版本的切块和向量，做 BM25 与向量的混合排序。
- `document` Artifact 也可以通过通用 Artifact 接口读取、遗忘和恢复。

## 测试

```bash
uv run pytest tests/e2e/test_knowledge_base_plugin.py
```
