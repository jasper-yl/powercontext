/*
Copyright (c) 2026 OceanBase.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Knowledge base management page. Every call goes through the Dashboard host page to the
// plugin's own routes; the page itself never holds a credential.
(async function () {
  "use strict";

  const TEXT = {
    zh: {
      knowledge_bases: "知识库", new: "新建", name: "名称", description: "描述", cancel: "取消", create: "创建",
      save: "保存", edit: "编辑", delete: "删除", confirm_delete: "确认删除", no_knowledge_bases: "还没有知识库。",
      welcome_title: "选择或新建一个知识库", welcome_text: "每个知识库独立保存文档、切块和检索索引。批量上传文档后，统一选择解析方式再处理。",
      documents: "文档", search_and_ask: "检索与问答", upload_title: "拖拽文件到这里，或点击选择文件",
      upload_hint: "支持 {formats}，单个文件不超过 {size}", document: "文档", status: "状态",
      chunks: "切块", revision: "版本", size: "大小", no_documents: "还没有文档。", download: "下载",
      status_ready: "就绪", status_processing: "处理中", status_failed: "失败", question_placeholder: "输入问题或检索词…",
      search_in: "检索范围", mode: "检索方式", mode_hybrid: "混合", mode_keyword: "关键词", mode_vector: "向量",
      search: "检索", ask: "提问", answer: "回答", no_hits: "没有找到相关内容。",
      no_generation: "Server 未配置生成模型，下面是检索到的片段。", embedding: "向量：{profile}",
      embedding_hash: "向量：内置哈希（未配置 embedding 模型）", generation_on: "生成模型：已配置", generation_off: "生成模型：未配置",
      uploaded: "已上传 {name}，正在处理", created: "已创建知识库 {name}", deleted: "已删除", saved: "已保存",
      forbidden: "没有权限执行该操作。创建和删除知识库需要 server.admin。", error: "操作失败：{message}",
      too_large: "文件超过大小限制", unsupported: "不支持的文件类型", stopped: "页面已停止转发请求，请重新打开。",
      status_pending_config: "待配置", pending_title: "{count} 个文档等待选择解析方式", default_parser: "默认解析方式",
      parser_none: "不设默认：上传后等待配置", make_default: "同时设为该知识库的默认解析方式（之后的上传自动处理）",
      start_processing: "开始处理", processing_started: "已提交 {count} 个文档", uploaded_pending: "已上传 {name}，等待选择解析方式",
      view_chunks: "切块", close: "关闭", reparse_title: "换一种解析方式重新处理", reparse: "重新解析",
      restore_previous: "恢复上一版本", save_chunks: "保存修改", add_chunk: "在下方插入", remove_chunk: "删除",
      edited: "已人工编辑", page: "第 {page} 页", chunk_meta: "第 {revision} 版 · {parser} · {count} 个切块",
      chunks_saved: "已保存为第 {revision} 版", restored: "已恢复到第 {revision} 版", reparse_started: "已提交重新解析",
      empty_chunk: "切块不能为空", unsaved: "有未保存的修改",
    },
    en: {
      knowledge_bases: "Knowledge bases", new: "New", name: "Name", description: "Description", cancel: "Cancel",
      create: "Create", save: "Save", edit: "Edit", delete: "Delete", confirm_delete: "Confirm delete",
      no_knowledge_bases: "No knowledge bases yet.", welcome_title: "Select or create a knowledge base",
      welcome_text: "Each knowledge base keeps its own documents, chunks, and search index. Upload documents in bulk, then choose how to parse them.",
      documents: "Documents", search_and_ask: "Search & ask", upload_title: "Drop files here, or click to choose",
      upload_hint: "{formats}, up to {size} each", document: "Document", status: "Status",
      chunks: "Chunks", revision: "Revision", size: "Size", no_documents: "No documents yet.", download: "Download",
      status_ready: "Ready", status_processing: "Processing", status_failed: "Failed",
      question_placeholder: "Ask a question or enter search terms…", search_in: "Search in", mode: "Mode",
      mode_hybrid: "Hybrid", mode_keyword: "Keyword", mode_vector: "Vector", search: "Search", ask: "Ask",
      answer: "Answer", no_hits: "Nothing relevant was found.",
      no_generation: "The Server has no generation model; these are the retrieved passages.",
      embedding: "Embedding: {profile}", embedding_hash: "Embedding: built-in hash (no embedding model)",
      generation_on: "Generation model: configured", generation_off: "Generation model: not configured",
      uploaded: "Uploaded {name}; processing", created: "Created {name}", deleted: "Deleted", saved: "Saved",
      forbidden: "You are not allowed to do this. Creating and deleting knowledge bases needs server.admin.",
      error: "Failed: {message}", too_large: "The file exceeds the size limit",
      unsupported: "This file type is not supported", stopped: "Requests stopped; reopen the page.",
      status_pending_config: "Needs parser", pending_title: "{count} documents are waiting for a parser",
      default_parser: "Default parser", parser_none: "No default: uploads wait for a parser",
      make_default: "Also make this the knowledge base's default (later uploads are processed automatically)",
      start_processing: "Start processing", processing_started: "Submitted {count} documents",
      uploaded_pending: "Uploaded {name}; choose a parser to process it", view_chunks: "Chunks", close: "Close",
      reparse_title: "Reprocess with another parser", reparse: "Reprocess", restore_previous: "Restore previous revision",
      save_chunks: "Save changes", add_chunk: "Insert below", remove_chunk: "Remove", edited: "Edited",
      page: "page {page}", chunk_meta: "Revision {revision} · {parser} · {count} chunks",
      chunks_saved: "Saved as revision {revision}", restored: "Restored revision {revision}",
      reparse_started: "Reprocessing submitted", empty_chunk: "Chunks cannot be empty", unsaved: "You have unsaved changes",
    },
  };

  const client = window.powercontextPluginClient();
  const context = await client.context();
  const t = TEXT[context.language] || TEXT.en;
  document.documentElement.lang = context.language === "zh" ? "zh-CN" : "en";
  document.documentElement.dataset.theme = context.theme === "dark" ? "dark" : "light";

  const $ = selector => document.querySelector(selector);
  const format = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
  const state = { info: null, parsers: [], bases: [], selected: null, targets: null, documents: [], poll: null, open: null };
  const lang = context.language === "zh" ? "zh" : "en";

  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t[node.dataset.i18n];
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) node.placeholder = t[node.dataset.i18nPlaceholder];

  function element(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) {
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.filter(child => child !== null && child !== undefined));
    return node;
  }

  let toastTimer;
  function toast(message, isError = false) {
    const node = $("#toast");
    node.textContent = message;
    node.className = isError ? "toast error" : "toast";
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 3000);
  }

  async function call(method, path, options = {}) {
    let response;
    try {
      response = await client.request(method, path, options);
    } catch (error) {
      toast(error.message === "network_error" ? format(t.error, { message: "network" }) : t.stopped, true);
      throw error;
    }
    if (!response.ok) {
      const code = response.json?.error?.code;
      const message = response.status === 403 ? t.forbidden
        : response.status === 413 ? t.too_large
        : response.status === 415 ? t.unsupported
        : format(t.error, { message: response.json?.error?.message || code || response.status });
      toast(message, true);
      throw Object.assign(new Error(message), { status: response.status });
    }
    return response;
  }

  const bytes = value => value == null ? "–"
    : value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1048576).toFixed(1)} MB`;

  // Confirm destructive actions inline; sandboxed pages have no dialogs.
  function confirmButton(button, action) {
    let armed = false;
    let timer;
    const label = button.textContent;
    button.addEventListener("click", async () => {
      if (!armed) {
        armed = true;
        button.textContent = t.confirm_delete;
        button.classList.add("confirming");
        timer = setTimeout(() => { armed = false; button.textContent = label; button.classList.remove("confirming"); }, 4000);
        return;
      }
      clearTimeout(timer);
      button.disabled = true;
      try { await action(); } finally { button.disabled = false; armed = false; button.textContent = label; button.classList.remove("confirming"); }
    });
  }

  // Models and permissions

  async function loadInfo() {
    state.info = (await call("GET", "/info")).json;
    const models = $("#models");
    const profile = state.info.embedding_profile;
    models.replaceChildren(
      element("span", { class: "badge plain" }, profile.startsWith("hash") ? t.embedding_hash : format(t.embedding, { profile })),
      element("span", { class: "badge plain" }, state.info.generation_available ? t.generation_on : t.generation_off),
    );
    $("#new-kb").hidden = !state.info.can_manage;
    $("#delete-kb").hidden = !state.info.can_manage;
    $("#upload-hint").textContent = format(t.upload_hint, {
      formats: state.info.formats.join(" "), size: bytes(state.info.max_upload_bytes),
    });
    $("#file-input").accept = state.info.formats.join(",");
    state.parsers = (await call("GET", "/parsers")).json.items;
    $("#default-parser").replaceChildren(
      element("option", { value: "" }, t.parser_none),
      ...state.parsers.map(parser => element("option", { value: parser.name }, parser.title[lang])),
    );
  }

  // A parser and its parameters; the plugin validates them and fills in defaults.
  function parserPicker(container, choice) {
    const select = element("select", {}, ...state.parsers.map(parser => element("option", { value: parser.name }, parser.title[lang])));
    const description = element("p", { class: "muted description" });
    const params = element("div", { class: "params" });
    function render(values = {}) {
      const parser = state.parsers.find(item => item.name === select.value);
      description.textContent = parser.description[lang];
      params.replaceChildren(...parser.parameters.map(item => element("label", {}, item.name,
        element("input", { type: "number", name: item.name, min: item.minimum, max: item.maximum, value: values[item.name] ?? item.default }))));
    }
    select.value = choice?.name || state.parsers[0].name;
    select.addEventListener("change", () => render());
    render(choice?.params);
    container.replaceChildren(select, description, params);
    return () => ({
      name: select.value,
      params: Object.fromEntries([...params.querySelectorAll("input")].map(input => [input.name, Number(input.value)])),
    });
  }
  const parserName = choice => state.parsers.find(item => item.name === choice?.name)?.title[lang] ?? choice?.name ?? "–";

  // Knowledge bases

  async function loadBases(select) {
    state.bases = (await call("GET", "/knowledge-bases")).json.items;
    if (select !== undefined) state.selected = select;
    if (!state.bases.some(base => base.id === state.selected)) state.selected = state.bases[0]?.id ?? null;
    await selectBase(state.selected);
  }

  function renderBases() {
    $("#kb-empty").hidden = state.bases.length > 0;
    $("#kb-list").replaceChildren(...state.bases.map(base => element("li", {},
      element("button", { type: "button", "aria-current": base.id === state.selected ? "true" : "false", onclick: () => selectBase(base.id) },
        base.name, base.description ? element("span", { class: "description" }, base.description) : null),
    )));
    renderTargets();
  }

  function current() { return state.bases.find(base => base.id === state.selected) || null; }

  async function selectBase(id) {
    // Searching starts from the knowledge base being viewed; more can be ticked afterwards.
    if (id !== state.selected) state.targets = null;
    state.selected = id;
    renderBases();
    const base = current();
    $("#placeholder").hidden = Boolean(base);
    $("#workspace").hidden = !base;
    $("#edit-kb-form").hidden = true;
    if (!base) return;
    $("#kb-name").textContent = base.name;
    $("#kb-description").textContent = base.description;
    await loadDocuments();
  }

  $("#new-kb").addEventListener("click", () => { $("#new-kb-form").hidden = false; $("#new-kb-form").name.focus(); });
  for (const form of document.querySelectorAll("form")) {
    form.querySelector("[data-cancel]")?.addEventListener("click", () => { form.hidden = true; form.reset(); });
  }

  $("#new-kb-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = { name: form.name.value.trim(), description: form.description.value.trim() };
    const created = (await call("POST", "/knowledge-bases", { json: body })).json;
    form.reset();
    form.hidden = true;
    toast(format(t.created, { name: created.name }));
    await loadBases(created.id);
  });

  $("#edit-kb").addEventListener("click", () => {
    const form = $("#edit-kb-form");
    const base = current();
    form.name.value = base.name;
    form.description.value = base.description;
    form.default_parser.value = base.default_parser?.name ?? "";
    form.hidden = false;
    form.name.focus();
  });

  $("#edit-kb-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    await call("PATCH", `/knowledge-bases/${encodeURIComponent(state.selected)}`, {
      json: {
        name: form.name.value.trim(),
        description: form.description.value.trim(),
        default_parser: form.default_parser.value ? { name: form.default_parser.value } : null,
      },
    });
    form.hidden = true;
    toast(t.saved);
    await loadBases(state.selected);
  });

  confirmButton($("#delete-kb"), async () => {
    await call("DELETE", `/knowledge-bases/${encodeURIComponent(state.selected)}`);
    toast(t.deleted);
    await loadBases(null);
  });

  // Documents

  async function loadDocuments() {
    const base = state.selected;
    if (!base) return;
    const page = (await call("GET", `/knowledge-bases/${encodeURIComponent(base)}/documents`)).json;
    if (base !== state.selected) return;
    state.documents = page.items;
    renderDocuments();
    clearTimeout(state.poll);
    if (state.documents.some(item => item.status === "processing")) state.poll = setTimeout(loadDocuments, 1500);
  }

  let pendingParser = null;
  function renderPending() {
    const pending = state.documents.filter(item => item.status === "pending_config").length;
    const form = $("#pending");
    if (!pending) { form.hidden = true; pendingParser = null; return; }
    $("#pending-title").textContent = format(t.pending_title, { count: pending });
    if (!pendingParser) pendingParser = parserPicker($("#pending-parser"), current()?.default_parser);
    form.hidden = false;
  }

  $("#pending").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const result = (await call("POST", `/knowledge-bases/${encodeURIComponent(state.selected)}/processing`, {
      json: { parser: pendingParser(), make_default: form.make_default.checked },
    })).json;
    toast(format(t.processing_started, { count: result.submitted }));
    pendingParser = null;
    await loadBases(state.selected);
  });

  function renderDocuments() {
    renderPending();
    $("#documents-empty").hidden = state.documents.length > 0;
    $("#documents").replaceChildren(...state.documents.map(item => {
      const chunks = element("button", { type: "button", class: "button link", disabled: item.status !== "ready" }, t.view_chunks);
      chunks.addEventListener("click", () => openChunks(item));
      const download = element("button", { type: "button", class: "button link", disabled: !item.revision }, t.download);
      download.addEventListener("click", () => downloadDocument(item));
      const remove = element("button", { type: "button", class: "button link danger" }, t.delete);
      confirmButton(remove, async () => {
        await call("DELETE", `/knowledge-bases/${encodeURIComponent(state.selected)}/documents/${encodeURIComponent(item.id)}`);
        toast(t.deleted);
        await loadDocuments();
      });
      return element("tr", {},
        element("td", { class: "name", title: item.error || "" }, item.name,
          item.edited ? element("span", { class: "badge plain edited" }, t.edited) : null,
          item.error ? element("div", { class: "muted small" }, item.error) : null),
        element("td", {}, element("span", { class: `badge ${item.status}` }, t[`status_${item.status}`])),
        element("td", { class: "num" }, item.chunk_count ?? "–"),
        element("td", { class: "num" }, item.revision ?? "–"),
        element("td", { class: "num" }, bytes(item.size)),
        element("td", { class: "actions" }, chunks, download, remove),
      );
    }));
  }

  async function downloadDocument(item) {
    const response = await call("GET", `/knowledge-bases/${encodeURIComponent(state.selected)}/documents/${encodeURIComponent(item.id)}/content`);
    const url = URL.createObjectURL(response.blob);
    const link = element("a", { href: url, download: response.filename || item.name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  async function upload(files) {
    // Every file of one drop goes to the knowledge base that was open when it started.
    const base = state.selected;
    for (const file of files) {
      if (!state.info.formats.some(suffix => file.name.toLowerCase().endsWith(suffix))) { toast(t.unsupported, true); continue; }
      if (file.size > state.info.max_upload_bytes) { toast(t.too_large, true); continue; }
      await call("POST", `/knowledge-bases/${encodeURIComponent(base)}/documents`, {
        query: { name: file.name },
        body: await file.arrayBuffer(),
        contentType: file.type || "application/octet-stream",
      }).then(response => toast(format(response.json.status === "pending_config" ? t.uploaded_pending : t.uploaded, { name: file.name })))
        .catch(() => {});
    }
    await loadDocuments();
  }

  const dropzone = $("#dropzone");
  $("#file-input").addEventListener("change", event => { upload([...event.target.files]); event.target.value = ""; });
  dropzone.addEventListener("dragover", event => { event.preventDefault(); dropzone.classList.add("over"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
  dropzone.addEventListener("drop", event => { event.preventDefault(); dropzone.classList.remove("over"); upload([...event.dataTransfer.files]); });

  // Chunks: read the current revision's chunk set, edit it, and save it as a new revision.

  let chunkParser = null;
  let dirty = false;

  async function openChunks(item) {
    const path = `/knowledge-bases/${encodeURIComponent(state.selected)}/documents/${encodeURIComponent(item.id)}`;
    const view = (await call("GET", `${path}/chunks`)).json;
    state.open = { item, path, view };
    dirty = false;
    $("#chunk-title").textContent = item.name;
    $("#chunk-meta").textContent = format(t.chunk_meta, { revision: view.revision, parser: parserName(view.parser), count: view.chunks.length })
      + (view.edited ? ` · ${t.edited}` : "");
    $("#chunk-restore").disabled = view.revision <= 1;
    chunkParser = parserPicker($("#chunk-parser"), view.parser);
    renderChunks(view.chunks.map(chunk => ({ text: chunk.text, page: chunk.page })));
    $("#chunk-panel").hidden = false;
  }

  function chunkItem(chunk) {
    const text = element("textarea", { rows: Math.min(12, Math.max(3, Math.ceil(chunk.text.length / 70))) });
    text.value = chunk.text;
    text.addEventListener("input", () => { dirty = true; });
    const insert = element("button", { type: "button", class: "button link" }, t.add_chunk);
    const remove = element("button", { type: "button", class: "button link danger" }, t.remove_chunk);
    const item = element("li", { "data-page": chunk.page ?? "" },
      element("div", { class: "chunk-head" }, element("span", { class: "number" }),
        chunk.page ? element("span", {}, format(t.page, { page: chunk.page })) : null,
        element("span", { class: "spacer" }), insert, remove),
      text);
    insert.addEventListener("click", () => {
      const added = chunkItem({ text: "", page: chunk.page });
      item.after(added);
      numberChunks();
      added.querySelector("textarea").focus();
      dirty = true;
    });
    remove.addEventListener("click", () => { item.remove(); numberChunks(); dirty = true; });
    return item;
  }

  function numberChunks() {
    for (const [index, node] of [...document.querySelectorAll("#chunk-list .number")].entries()) node.textContent = `#${index + 1}`;
  }

  function renderChunks(chunks) {
    $("#chunk-list").replaceChildren(...chunks.map(chunkItem));
    numberChunks();
  }

  function closeChunks() {
    $("#chunk-panel").hidden = true;
    state.open = null;
  }

  $("#chunk-close").addEventListener("click", closeChunks);
  $("#chunk-panel").addEventListener("click", event => { if (event.target === event.currentTarget && !dirty) closeChunks(); });

  $("#chunk-save").addEventListener("click", async () => {
    const chunks = [...document.querySelectorAll("#chunk-list li")].map(node => ({
      text: node.querySelector("textarea").value.trim(),
      page: node.dataset.page ? Number(node.dataset.page) : null,
    }));
    if (!chunks.length || chunks.some(chunk => !chunk.text)) { toast(t.empty_chunk, true); return; }
    const { item, path, view } = state.open;
    const saved = (await call("PUT", `${path}/chunks`, { json: { expected_revision: view.revision, chunks } })).json;
    toast(format(t.chunks_saved, { revision: saved.revision }));
    await loadDocuments();
    await openChunks({ ...item, revision: saved.revision });
  });

  $("#chunk-restore").addEventListener("click", async () => {
    const { item, path, view } = state.open;
    await call("POST", `${path}/restorations`, { json: { revision: view.revision - 1 } });
    toast(format(t.restored, { revision: view.revision - 1 }));
    await loadDocuments();
    await openChunks(item);
  });

  $("#chunk-reparse").addEventListener("click", async () => {
    const { item } = state.open;
    await call("POST", `/knowledge-bases/${encodeURIComponent(state.selected)}/processing`, {
      json: { parser: chunkParser(), document_ids: [item.id] },
    });
    toast(t.reparse_started);
    closeChunks();
    await loadDocuments();
  });

  // Search and ask

  for (const tab of document.querySelectorAll("[data-tab]")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll("[data-tab]")) other.setAttribute("aria-selected", String(other === tab));
      for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== tab.dataset.tab;
    });
  }

  function renderTargets() {
    const chosen = new Set(state.targets ?? (state.selected ? [state.selected] : []));
    $("#targets").replaceChildren(...state.bases.map(base => element("label", {},
      element("input", { type: "checkbox", value: base.id, checked: chosen.has(base.id) }), base.name)));
  }

  $("#targets").addEventListener("change", () => {
    state.targets = [...document.querySelectorAll("#targets input:checked")].map(input => input.value);
  });

  function renderAnswer(text) {
    const node = $("#answer");
    if (text === undefined) { node.hidden = true; return; }
    node.hidden = false;
    const body = element("div");
    // Show citation markers such as [2] as highlighted references to the passages below.
    for (const part of (text ?? t.no_generation).split(/(\[\d+\])/)) {
      body.append(/^\[\d+\]$/.test(part) ? element("span", { class: "cite" }, part) : part);
    }
    node.replaceChildren(element("span", { class: "label" }, t.answer), body);
  }

  function renderHits(hits) {
    $("#hits").replaceChildren(...(hits.length ? hits.map(hit => element("li", { class: "card" },
      element("div", { class: "meta" },
        element("strong", {}, hit.document_name),
        hit.page ? element("span", { class: "page" }, format(t.page, { page: hit.page })) : null,
        element("span", { class: "muted" }, state.bases.find(base => base.id === hit.knowledge_base_id)?.name || ""),
        element("span", { class: "badge plain" }, hit.score.toFixed(3))),
      element("div", { class: "text" }, hit.text),
    )) : [element("li", { class: "muted" }, t.no_hits)]));
  }

  $("#ask-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const action = event.submitter?.value || "search";
    const ids = [...document.querySelectorAll("#targets input:checked")].map(input => input.value);
    const text = form.question.value.trim();
    if (!text || !ids.length) return;
    const buttons = form.querySelectorAll("button");
    for (const button of buttons) button.disabled = true;
    try {
      if (action === "ask") {
        const result = (await call("POST", "/ask", { json: { question: text, knowledge_base_ids: ids } })).json;
        renderAnswer(result.citations.length ? result.answer : t.no_hits);
        renderHits(result.citations);
      } else {
        const result = (await call("POST", "/search", { json: { query: text, knowledge_base_ids: ids, mode: form.mode.value } })).json;
        renderAnswer(undefined);
        renderHits(result.hits);
      }
    } finally {
      for (const button of buttons) button.disabled = false;
    }
  });

  await loadInfo();
  await loadBases();
})();
