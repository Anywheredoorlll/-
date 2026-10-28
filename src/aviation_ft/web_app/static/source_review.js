// The source editor is bound to one job/page and never applies a late response elsewhere.
let sourceEditorSequence = 0;
const sourceDrafts = new Map();

async function openSourceReview() {
  const jobId = state.jobId;
  const docId = $("#page-doc").value;
  const page = Number($("#page-number").value);
  const sequence = ++sourceEditorSequence;
  const pageSequence = ++pageRequestSequence;
  const host = $("#page-workspace");
  const belongs = () => sequence === sourceEditorSequence && pageSequence === pageRequestSequence && state.jobId === jobId &&
    state.view === "evidence" && $("#page-workspace") === host &&
    $("#page-doc")?.value === docId && Number($("#page-number")?.value) === page;
  host.innerHTML = loading();
  try {
    const data = await api(`/api/jobs/${jobId}/page-source?doc_id=${encodeURIComponent(docId)}&page=${page}`);
    if (!belongs()) return;
    renderSourceEditor(host, data, {jobId, docId, page, belongs});
  } catch (error) {
    if (belongs()) host.textContent = error.message;
  }
}

function sourceDiff(diff) {
  if (!diff) return '<p>缺少可比较的两份候选；不会以空白代替未生成的 OCR。</p>';
  return `<p>规范化差异率 ${(diff.difference_rate * 100).toFixed(1)}%（提示值，不是准确率）。忽略空白与换行进行比较，原始文字保留。</p>` +
    (diff.changes.length ? diff.changes.map((change, index) => `<article class="source-change ${change.highlights.length ? "source-important" : ""}">
      <strong>${index + 1}. ${{insert:"增加",delete:"删除",replace:"替换"}[change.type]} ${esc(change.highlights.join(" / "))}</strong>
      <div class="source-diff-columns">${["left", "right"].map((side) => `<div><small>${side === "left" ? "左侧" : "右侧"} · ${change[side].precision === "block" ? "块级定位" : "页级定位"}</small>
      <pre>${esc(change[side].text)}</pre>${change[side].locations.map(loc => `<button type="button" class="secondary source-locate" data-box="${esc(JSON.stringify(loc.bbox))}" title="${esc(loc.block_id)}">定位 ${esc(loc.block_id)}</button>`).join("")}</div>`).join("")}</div></article>`).join("") : "<p>比较结果没有非空白差异。</p>");
}

function renderSourceEditor(host, data, context) {
  const {jobId, docId, page, belongs} = context;
  const current = data.current;
  const kindLabel = {native:"原生提取",ocr:"OCR 整页输出",manual:"人工校正",legacy:"历史提取，来源未知"};
  const draftKey = `${jobId}:${docId}:${page}`;
  const draft = sourceDrafts.get(draftKey) || {};
  const statusLabel = {available:"可用",not_generated:"未生成",failed:"生成失败"};
  const panel = (kind) => {
    const item = data.candidates[kind];
    return `<section><h3>${kindLabel[kind]} · ${item ? statusLabel[item.status] : "缺少历史提取候选"}</h3>
      ${kind === "ocr" ? "<p>沿用已有 OCR 输出，可能同时包含原生文字。</p>" : ""}
      ${item?.status === "failed" ? `<p>${esc(item.error)}</p>` : ""}
      ${item?.status === "available" ? `<pre>${esc(item.blocks.map(b => b.text).join("\n"))}</pre>${!item.blocks.length ? "<p>已生成：提取文本为空</p>" : ""}` : ""}</section>`;
  };
  host.innerHTML = `<p><button id="source-back" class="secondary">返回语义段落</button></p>
    <p>当前使用：<strong>${esc(kindLabel[current.source_kind])}</strong> · 版本 <code>${esc(current.source_revision_id || "尚未建立")}</code> · ${data.source_verified ? "已人工核实" : "未核实"}</p>
    <p class="source-risk">${data.risks.map(esc).join("；")}</p>
    <p>“选择来源”改变实际使用的整页结果；“保存校正”产生人工校正版本。两者都不等于完成核实。处理依据只解释操作。</p>
    <div class="split-view"><div><iframe class="pdf-frame" src="/api/jobs/${jobId}/pdf/${encodeURIComponent(docId)}#page=${page}&view=FitH"></iframe>
      <div class="source-page-map" style="aspect-ratio:${data.width || 1}/${data.height || 1}" title="PDF 块区域示意（块级精度）"><span class="source-box"></span></div><small>上图为 PDF 页面坐标示意，矩形仅代表块级区域。</small></div>
      <div class="source-panels"><details open><summary>当前有效文本</summary><pre>${esc(current.blocks.map(b => b.text).join("\n"))}</pre></details>
      <details><summary>原生提取</summary>${panel("native")}</details><details><summary>OCR 提取</summary>${panel("ocr")}</details>
      <details open><summary>原生 → OCR 具体差异</summary><div class="source-diff">${sourceDiff(data.comparison)}</div></details>
      <form id="source-form"><label class="stack-label">操作<select class="field" name="action">
      ${current.source_revision_id ? '<option value="select_source">选择整页来源</option><option value="correct_text">人工文字校正</option><option value="verify">核实当前来源</option>' : '<option value="initialize">显式建立历史来源基线</option>'}</select></label>
      <label class="stack-label" id="source-candidate-row">已有候选<select name="candidate_id" class="field">${Object.entries(data.candidates).filter(([, value]) => value.status === "available").map(([key, value]) => `<option value="${value.candidate_id}">${kindLabel[key]}${value.blocks.length ? "" : "（空文本）"}</option>`).join("")}</select></label>
      <label class="stack-label" id="source-block-row">当前来源中的单个原始块<select name="block_id" class="field">${current.blocks.map(b => `<option value="${esc(b.block_id)}">${esc(b.block_id)}</option>`).join("")}</select></label>
      <div id="source-correction"><p>修改前</p><pre id="source-before"></pre><label class="stack-label">修改后<textarea name="corrected_text"></textarea></label></div>
      <label class="stack-label">操作人<input name="reviewer" class="field" required></label><label class="stack-label">处理依据<textarea name="reason" required></textarea></label>
      <p><button type="button" class="secondary" id="source-preview">预览修改与影响</button> <button class="primary" id="source-save" disabled>确认保存</button></p>
      <p id="source-message"></p><div id="source-preview-result"></div></form>
      ${data.frozen ? '<p>任务已冻结，请复制后修改。</p><button class="secondary" id="source-clone">复制为新任务</button>' : ""}</div></div>`;
  $("#source-back", host).onclick = () => { ++sourceEditorSequence; loadPage(); };
  const form = $("#source-form", host);
  const field = name => form.elements.namedItem(name);
  for (const name of ["action", "candidate_id", "block_id", "corrected_text", "reviewer", "reason"]) {
    if (draft[name] !== undefined) field(name).value = draft[name];
  }
  let previewPayload = null;
  let pending = null;
  function remember() {
    sourceDrafts.set(draftKey, Object.fromEntries(new FormData(form)));
    previewPayload = null;
    pending = null;
    $("#source-save", host).disabled = true;
  }
  function locate(box) {
    const marker = $(".source-box", host);
    marker.style.cssText = `display:block;left:${100*box[0]/data.width}%;top:${100*box[1]/data.height}%;width:${100*(box[2]-box[0])/data.width}%;height:${100*(box[3]-box[1])/data.height}%`;
  }
  function wireLocations() {
    $$(".source-locate", host).forEach(button => button.onclick = () => locate(JSON.parse(button.dataset.box)));
  }
  function updateBlock(reset) {
    const block = current.blocks.find(b => b.block_id === field("block_id").value);
    $("#source-before", host).textContent = block?.text || "";
    if (reset) field("corrected_text").value = block?.text || "";
    if (block) locate(block.bbox);
  }
  function updateAction() {
    const action = field("action").value;
    $("#source-candidate-row", host).hidden = action !== "select_source";
    $("#source-block-row", host).hidden = action !== "correct_text";
    $("#source-correction", host).hidden = action !== "correct_text";
    $("#source-save", host).textContent = action === "correct_text" ? "确认保存校正" : action === "select_source" ? "确认选择来源" : action === "verify" ? "确认核实当前版本" : "确认建立基线";
  }
  updateBlock(draft.corrected_text === undefined);
  updateAction(); wireLocations();
  form.oninput = remember;
  field("action").onchange = () => {remember(); updateAction();};
  field("block_id").onchange = () => {updateBlock(true); remember();};
  function payload() {
    const action = field("action").value;
    return {operation_type:action, doc_id:docId, pdf_page:page,
      expected_revision:current.source_revision_id || data.legacy_token,
      candidate_id:action === "correct_text" ? current.source_candidate_id : field("candidate_id").value,
      block_id:field("block_id").value, corrected_text:field("corrected_text").value,
      reviewer:field("reviewer").value, reason:field("reason").value};
  }
  const post = (action, body) => api(`/api/jobs/${jobId}/page-source/${action}`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  $("#source-preview", host).onclick = async () => {
    const body = payload();
    try {
      const result = await post("preview", body);
      if (!belongs() || JSON.stringify(body) !== JSON.stringify(payload())) return;
      previewPayload = body;
      $("#source-preview-result", host).innerHTML = `<h3>当前 → 预览</h3>${sourceDiff(result.comparison)}<p>${esc(result.impact.policy)}：语义单元 ${result.impact.semantic_ids.length}；证据 ${result.impact.evidence_count}；问答 ${result.impact.sample_count}；TXT ${result.impact.txt ? "有" : "无"}；导出 ${result.impact.exports.length}。</p><pre>${esc(JSON.stringify(result.impact, null, 2))}</pre>`;
      $("#source-save", host).disabled = data.frozen;
      wireLocations();
    } catch (error) {if (belongs()) $("#source-message", host).textContent = error.message;}
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (!previewPayload || JSON.stringify(previewPayload) !== JSON.stringify(payload())) return;
    pending ||= {...previewPayload, operation_id:crypto.randomUUID()};
    const action = {select_source:"select",correct_text:"correct",initialize:"initialize",verify:"verify"}[pending.operation_type];
    $("#source-save", host).disabled = true;
    try {
      const result = await post(action, pending);
      if (!belongs()) return;
      sourceDrafts.delete(draftKey);
      const refreshed = await api(`/api/jobs/${jobId}/evidence`);
      if (!belongs()) return;
      state.evidence = refreshed; state.candidates = null;
      const message = `${result.summary} · 版本 ${result.current_revision} · 影响证据 ${result.impact.evidence_count}、问答 ${result.impact.sample_count}。${result.next_step}`;
      await openSourceReview();
      const messageElement = $("#source-message");
      if (state.jobId === jobId && messageElement && $("#page-doc")?.value === docId && Number($("#page-number")?.value) === page) messageElement.textContent = message;
    } catch (error) {
      if (belongs()) {
        $("#source-message", host).textContent = `${error.message}。草稿已保留；可重新打开本页比较，勿自动覆盖。`;
        $("#source-save", host).disabled = false;
      }
    }
  };
  if (data.frozen) {
    $$("input, textarea, select, #source-preview", form).forEach(el => el.disabled = true);
    $("#source-clone", host).onclick = async () => {
      try {
        const result = await api(`/api/jobs/${jobId}/clone`, {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
        if (!belongs()) return;
        state.evidence = null; state.candidates = null;
        await loadJobs(result.job_id);
      } catch (error) {if (belongs()) toast(error.message, true);}
    };
  }
}
