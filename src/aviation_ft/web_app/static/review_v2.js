/* Conservative, version-aware review UI. The server enforces every approval gate. */
state.requestEpoch = 0;
state.reviewPage = 1;
state.reviewStatus = "all";
state.reviewDoc = "";
state.reviewRisk = "";
state.providerRole = "generator";
state.providerRoleConfig = null;
state.detailRequestId = 0;
state.reviewRequestId = 0;

loadJobs = async function(preferred) {
  state.jobs = await api("/api/jobs");
  const select = $("#job-select");
  select.innerHTML = state.jobs.length ? state.jobs.map(j => `<option value="${j.job_id}">${esc(j.label || "未命名任务")} · ${j.job_id.slice(0,8)}</option>`).join("") : '<option value="">暂无任务</option>';
  const currentExists = state.jobs.some(job => job.job_id === state.jobId);
  const next = preferred || (currentExists ? state.jobId : null) || state.jobs[0]?.job_id || null;
  $("#delete-job-button").disabled = !next;
  if (next !== state.jobId) await changeJob(next);
  else { select.value = next || ""; await loadDashboard(); }
};

async function changeJob(jobId) {
  state.jobId = jobId;
  state.requestEpoch++;
  state.evidence = null;
  state.candidates = null;
  state.selectedCandidate = null;
  state.detailRequestId++;
  state.reviewRequestId++;
  state.providerRole = "generator";
  state.providerRoleConfig = null;
  $("#job-select").value = jobId;
  await loadDashboard();
}

const originalApi = api;
api = async function(path, options = {}) {
  const job = path.match(/^\/api\/jobs\/([0-9a-f]{32})/i)?.[1];
  const result = await originalApi(path, options);
  if (job && job !== state.jobId) throw new Error("任务已切换，旧响应已丢弃");
  return result;
};

loadDashboard = async function() {
  const job = state.jobId, epoch = ++state.requestEpoch;
  if (!job) { state.dashboard = null; renderEmpty(); return; }
  const data = await api(`/api/jobs/${job}`);
  if (epoch !== state.requestEpoch || job !== state.jobId) return;
  state.dashboard = data;
  syncProviderChrome();
  await render();
};

renderIssues = function() {
  const issues = state.evidence.quality_issues || [];
  const rows = issues.map(x => `<tr><td><a href="/api/jobs/${state.jobId}/pdf/${esc(x.doc_id)}#page=${x.pdf_page}" target="_blank">${esc(x.pdf_page)} 页 PDF</a></td><td>${esc(x.severity)}</td><td>${esc(x.message)}</td><td>${esc(x.status)}</td><td>${x.status === "待处理" ? `<button class="resolve-issue secondary" data-id="${esc(x.issue_id)}">核实</button>` : ""}</td></tr>`).join("");
  $("#evidence-panel").innerHTML = `<section class="card card-pad"><div class="section-head"><strong>逐页提取问题</strong><span>保留处理历史</span></div>${rows ? `<div class="table-wrap"><table><thead><tr><th>PDF 页</th><th>级别</th><th>说明</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : "暂无待处理问题"}</section>`;
  $$(".resolve-issue").forEach(button => button.onclick = async () => {
    const reviewer = prompt("核对人 ID"), reason = prompt("对照 PDF 后的处理依据");
    if (!reviewer || !reason) return;
    try {
      await api(`/api/jobs/${state.jobId}/quality-issues/${button.dataset.id}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({status:"已核实", reviewer, reason})});
      state.evidence = null; await renderEvidence();
    } catch (error) { toast(error.message, true); }
  });
};

renderLibrary = function() {
  const e = state.evidence;
  const rows = e.units.map(x => `<tr><td><code>${esc(x.evidence_unit_id)}</code></td><td><a href="/api/jobs/${state.jobId}/pdf/${esc(x.doc_id)}#page=${x.pdf_page}" target="_blank">${esc(x.filename)} · ${x.pdf_page}</a></td><td>${esc(x.quote?.slice(0,100))}</td><td>${esc(x.source_verification?.status || "legacy_unverified")}</td><td>${!e.frozen && x.source_verification?.status !== "source_verified" ? `<button class="verify-source secondary" data-id="${esc(x.evidence_unit_id)}">对照 PDF 核实</button>` : ""}</td></tr>`).join("");
  $("#evidence-panel").innerHTML = `<section class="card card-pad"><div class="section-head"><strong>证据库</strong><button id="freeze-evidence" class="primary" ${e.frozen || !e.units.length ? "disabled" : ""}>冻结证据分区</button></div><div class="table-wrap"><table><thead><tr><th>ID</th><th>来源</th><th>引文</th><th>来源状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  $("#freeze-evidence").onclick = freezeEvidence;
  $$(".verify-source").forEach(button => button.onclick = async () => {
    const reviewer = prompt("已对照 PDF 的核对人 ID"), reason = prompt("核对的页码、文字或表格单元格及依据");
    if (!reviewer || !reason) return;
    try {
      await api(`/api/jobs/${state.jobId}/evidence/${button.dataset.id}/verify`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({reviewer, reason})});
      state.evidence = null; await renderEvidence();
    } catch (error) { toast(error.message, true); }
  });
};

const originalRenderEvidence = renderEvidence;
renderEvidence = async function() {
  await originalRenderEvidence();
  const tabs = $("#evidence-tabs");
  if (!tabs || tabs.querySelector('[data-tab="cross"]')) return;
  const button = document.createElement("button");
  button.className = "tab";
  button.dataset.tab = "cross";
  button.textContent = "跨页建议";
  tabs.insertBefore(button, tabs.querySelector('[data-tab="library"]'));
  button.onclick = () => {
    $$("[data-tab]").forEach(x => x.classList.toggle("active", x === button));
    renderCrossPage();
  };
};

async function renderCrossPage() {
  const job = state.jobId, epoch = state.requestEpoch;
  const proposals = await api(`/api/jobs/${job}/evidence/cross-page-proposals`);
  if (job !== state.jobId || epoch !== state.requestEpoch) return;
  $("#evidence-panel").innerHTML = `<section class="card card-pad"><h3>跨页续接建议</h3><p>只有连续正文建议可人工确认；保存后仍需逐页对照 PDF 核实证据来源。</p>${proposals.map((p, i) => `<div class="source-card"><strong>${esc(p.filename)} · 第 ${p.start_page}-${p.end_page} 页</strong><p>${esc(p.start_text)}\n\n${esc(p.end_text)}</p><small>${esc(p.decision)} · ${esc(p.reasons.join("、"))}</small>${p.decision === "proposed" && !state.evidence.frozen ? `<button class="confirm-cross secondary" data-index="${i}">确认连续正文并保存草稿</button>` : ""}</div>`).join("") || "暂无跨页续接建议"}</section>`;
  $$(".confirm-cross").forEach(button => button.onclick = async () => {
    const p = proposals[+button.dataset.index];
    const reviewer = prompt("核对人 ID"), reason = prompt("对照 PDF 后确认续接的依据");
    const topic = prompt("主题"), clause_group = prompt("章节或相邻条款组"), split = prompt("数据分区：train / validation / test", "train");
    if (!reviewer || !reason || !topic || !clause_group || !split) return;
    try {
      await api(`/api/jobs/${job}/evidence/cross-page-confirm`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({start_semantic_id:p.start_semantic_id, end_semantic_id:p.end_semantic_id, topic, clause_group, split, reviewer, reason})});
      toast("跨页证据草稿已保存，请继续核实来源并重建 TXT");
      state.evidence = null; await renderEvidence();
    } catch (error) { toast(error.message, true); }
  });
}

renderReview = async function() {
  const job = state.jobId, epoch = state.requestEpoch, requestId = ++state.reviewRequestId;
  const query = new URLSearchParams({page:String(state.reviewPage), page_size:"25", status:state.reviewStatus, doc_id:state.reviewDoc, risk:state.reviewRisk});
  const data = await api(`/api/jobs/${job}/candidates?${query}`);
  if (job !== state.jobId || epoch !== state.requestEpoch || requestId !== state.reviewRequestId) return;
  state.candidates = data;
  const rows = data.items.map(x => `<tr><td><button class="open-candidate secondary" data-id="${esc(x.sample_id)}">查看</button></td><td><code>${esc(x.sample_id)}</code></td><td>${esc(x.question)}</td><td>${esc(x.review_status)}</td><td>${esc(x.flags.join("、"))}</td></tr>`).join("");
  $("#content").innerHTML = head("问答审核", "逐断言核对答案、证据和 PDF 页面") + `<section class="card card-pad"><div class="toolbar"><label>状态<select id="review-filter" class="field"><option value="all">全部</option>${["pending","pending_expert_review","approved","approved_after_edit","rejected"].map(x => `<option value="${x}" ${state.reviewStatus === x ? "selected" : ""}>${x}</option>`).join("")}</select></label><label>文档 ID<input id="review-doc" class="field" value="${esc(state.reviewDoc)}"></label><label>风险<input id="review-risk" class="field" value="${esc(state.reviewRisk)}"></label><button id="review-filter-go" class="secondary">筛选</button></div><div class="table-wrap"><table><thead><tr><th></th><th>样本</th><th>问题</th><th>状态</th><th>风险</th></tr></thead><tbody>${rows}</tbody></table></div><div class="button-row"><button id="review-prev" class="secondary" ${state.reviewPage <= 1 ? "disabled" : ""}>上一页</button><span>第 ${data.page} 页 · 共 ${data.total} 条</span><button id="review-next" class="secondary" ${data.page * data.page_size >= data.total ? "disabled" : ""}>下一页</button></div></section><section id="candidate-detail"></section>`;
  $("#review-filter-go").onclick = () => { state.reviewPage = 1; state.reviewStatus = $("#review-filter").value; state.reviewDoc = $("#review-doc").value.trim(); state.reviewRisk = $("#review-risk").value.trim(); renderReview(); };
  $("#review-prev").onclick = () => { state.reviewPage--; renderReview(); };
  $("#review-next").onclick = () => { state.reviewPage++; renderReview(); };
  $$(".open-candidate").forEach(button => button.onclick = () => openCandidate(button.dataset.id));
};

async function openCandidate(sampleId) {
  const job = state.jobId, epoch = state.requestEpoch, detailId = ++state.detailRequestId;
  const r = await api(`/api/jobs/${job}/candidates/${sampleId}`);
  if (job !== state.jobId || epoch !== state.requestEpoch || detailId !== state.detailRequestId || !$("#candidate-detail")) return;
  state.selectedCandidate = r;
  const claims = r.claims.map(c => `<li><button class="claim-link secondary" data-id="${esc(c.claim_id)}">${esc(c.claim_id)} · ${esc(c.text)}</button><small>答案片段：${esc(c.answer_quote || "旧数据未标注")}</small>${(c.supporting_quotes || []).map(s => `<button class="claim-source-link secondary" data-evidence="${esc(s.evidence_id)}">${esc(s.evidence_id)} 原文：${esc(s.quote)}</button>`).join("")}<label><input class="claim-check" type="checkbox" data-id="${esc(c.claim_id)}"> 已逐项核实该事实和支持片段</label></li>`).join("");
  const sources = r.source_units.map(u => `<div class="source-card" data-unit="${esc(u?.evidence_unit_id)}"><strong>${esc(u?.evidence_unit_id || "来源缺失")}</strong><p>${esc(u?.quote)}</p><small>${esc(u?.source_verification?.status || "legacy_unverified")}</small>${(u?.source_locations || []).map(loc => `<button class="source-page secondary" data-doc="${esc(u.doc_id)}" data-page="${loc.pdf_page}" data-unit="${esc(u.evidence_unit_id)}">PDF 第 ${loc.pdf_page} 页 · 块级定位</button>`).join("")}<label><input class="source-check" type="checkbox" data-id="${esc(u?.evidence_unit_id)}"> 已对照 PDF 核实该来源</label></div>`).join("");
  const numericInput = r.numeric_checks?.status === "needs_human" ? `<div><p>逐项数值核对：</p>${(r.numeric_checks.answer_facts || []).map((fact, index) => `<label><input class="numeric-check" type="checkbox" data-index="${index}"> ${esc(fact.subject)} ${esc(fact.property)} ${esc(fact.operator)} ${esc(fact.value)} ${esc(fact.unit)}</label>`).join("")}<label>核对依据（对照 PDF）<input id="numeric-reason" class="field" placeholder="写明主体、属性、关系、单位及条件的核对依据"></label></div>` : "";
  $("#candidate-detail").innerHTML = `<section class="card card-pad"><h2>${esc(r.question)}</h2><p class="review-answer">${esc(r.answer)}</p><h3>事实断言</h3><ul>${claims || "无事实断言"}</ul><h3>引用与来源</h3>${sources}<div id="source-preview"></div><p>风险：${esc(r.quality_flags.join("、") || "无")}</p><p>数值：${esc(r.numeric_checks?.issues?.join("、") || r.numeric_checks?.status || "未检查")}</p><p>覆盖：${esc(r.coverage_check?.status || "未检查")}</p><p>评审轮次：${r.judge_assessments?.length || 0}；历史事件：${r.review_history.length}</p><div class="toolbar"><label>核对人<input id="detail-reviewer" class="field"></label><label>备注<input id="detail-comment" class="field"></label>${numericInput}</div><div class="button-row"><button class="review-action primary" data-decision="approved">逐项核实并通过</button><button class="review-action secondary" data-decision="pending_expert_review">待专家核对</button><button class="review-action secondary" data-decision="rejected">拒绝</button></div></section>`;
  $$(".source-page").forEach(button => button.onclick = () => showSourcePage(button.dataset.doc, +button.dataset.page, button.dataset.unit));
  $$(".claim-link").forEach(button => button.onclick = () => { const claim = r.claims.find(x => x.claim_id === button.dataset.id); const ref = r.evidence.find(x => x.evidence_id === claim?.evidence_ids?.[0]); const source = r.source_units.find(x => x?.evidence_unit_id === ref?.evidence_unit_id); const loc = source?.source_locations?.[0]; if (loc) showSourcePage(source.doc_id, loc.pdf_page, source.evidence_unit_id); });
  $$(".claim-source-link").forEach(button => button.onclick = () => { const ref = r.evidence.find(x => x.evidence_id === button.dataset.evidence); const source = r.source_units.find(x => x?.evidence_unit_id === ref?.evidence_unit_id); const card = $$(".source-card").find(x => x.dataset.unit === source?.evidence_unit_id); card?.scrollIntoView({behavior:"smooth", block:"center"}); const loc = source?.source_locations?.[0]; if (loc) showSourcePage(source.doc_id, loc.pdf_page, source.evidence_unit_id); });
  $$(".review-action").forEach(button => button.onclick = () => submitCandidateReview(button.dataset.decision));
}

async function showSourcePage(doc, page, unit) {
  const job = state.jobId, detailId = state.detailRequestId;
  const geometry = await api(`/api/jobs/${job}/page-geometry/${doc}/${page}`);
  if (job !== state.jobId || detailId !== state.detailRequestId || !$("#source-preview")) return;
  const boxes = geometry.locations.filter(x => x.evidence_unit_id === unit).map(x => `<div class="pdf-highlight" title="块级定位" style="left:${x.rect[0]*100}%;top:${x.rect[1]*100}%;width:${x.rect[2]*100}%;height:${x.rect[3]*100}%"></div>`).join("");
  $("#source-preview").innerHTML = `<p>第 ${page} 页 · 旋转 ${geometry.rotation}° · 块级定位</p><div class="pdf-image-wrap"><img src="/api/jobs/${job}/page-image/${doc}/${page}" alt="PDF 页面">${boxes}</div>`;
}

async function submitCandidateReview(decision) {
  const r = state.selectedCandidate;
  if (!r) return;
  const reviewer = $("#detail-reviewer").value.trim(), comment = $("#detail-comment").value.trim();
  if (!reviewer) return toast("请填写核对人", true);
  const claim_reviews = decision === "approved" ? $$(".claim-check:checked").map(x => ({claim_id:x.dataset.id, verdict:"supported"})) : [];
  const source_reviews = decision === "approved" ? $$(".source-check:checked").map(x => ({evidence_unit_id:x.dataset.id, verified:true})) : [];
  if (decision === "approved" && (claim_reviews.length !== r.claims.length || source_reviews.length !== r.evidence.length)) return toast("请逐项核实断言和来源", true);
  const numeric_reason = $("#numeric-reason")?.value.trim();
  if (decision === "approved" && r.numeric_checks?.status === "needs_human" && (!numeric_reason || numeric_reason.length < 10)) return toast("请填写数值核对依据", true);
  const checkedNumeric = $$(".numeric-check:checked").map(x => ({index:+x.dataset.index, verdict:"supported"}));
  if (decision === "approved" && r.numeric_checks?.status === "needs_human" && checkedNumeric.length !== (r.numeric_checks.answer_facts || []).length) return toast("请逐项核实数值事实", true);
  const numeric_review = numeric_reason ? {verdict:"verified", reason:numeric_reason,
    fact_reviews:checkedNumeric} : null;
  if (decision === "approved" && !confirm("已逐项对照答案、证据原文和 PDF 页面？")) return;
  try {
    await api(`/api/jobs/${state.jobId}/candidates/${r.sample_id}/review`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({decision, reviewer, comment, expected_revision:r.record_revision, claim_reviews, source_reviews, numeric_review})});
    toast("审核已保存"); await renderReview();
  } catch (error) { toast(error.message, true); }
}

const originalRenderProvider = renderProvider;
renderProvider = function() {
  const current = state.dashboard.provider;
  state.dashboard.provider = state.providerRoleConfig || current;
  try { originalRenderProvider(); } finally { state.dashboard.provider = current; }
  const selector = document.createElement("section");
  selector.className = "card card-pad";
  selector.innerHTML = `<label>模型角色 <select id="provider-role" class="field"><option value="generator">生成模型</option><option value="judge_primary">主评审模型</option><option value="judge_countercheck">反例评审模型</option></select></label><p>评审角色分别保存配置与密钥。自动通过默认关闭，需要离线校准和独立模型身份。</p>`;
  $("#content").prepend(selector);
  $("#provider-role").value = state.providerRole;
  $("#provider-role").onchange = async event => {
    const role = event.target.value, job = state.jobId;
    try {
      const config = await api(`/api/jobs/${job}/provider?role=${encodeURIComponent(role)}`);
      if (job !== state.jobId) return;
      state.providerRole = role;
      state.providerRoleConfig = config;
      renderProvider();
    } catch (error) { toast(error.message, true); }
  };
};

saveProvider = async function(event) {
  event.preventDefault();
  const job = state.jobId, role = state.providerRole, fd = new FormData(event.target);
  const payload = {preset_id:fd.get("preset_id"), base_url:"", model:fd.get("model"),
    api_key:fd.get("api_key"), privacy_acknowledged:fd.get("privacy_acknowledged") === "on"};
  try {
    state.providerRoleConfig = await api(`/api/jobs/${job}/provider?role=${role}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
    if (job !== state.jobId) return;
    toast("角色配置已保存");
    await loadDashboard();
  } catch (error) { toast(error.message, true); }
};

testProvider = async function() {
  try {
    toast("正在测试模型 API…");
    const result = await api(`/api/jobs/${state.jobId}/provider/test?role=${state.providerRole}`, {method:"POST"});
    toast(`连接成功：${result.model}`);
  } catch (error) { toast(error.message, true); }
};

useLocalProvider = async function() {
  const job = state.jobId;
  try {
    state.providerRoleConfig = await api(`/api/jobs/${job}/provider?role=${state.providerRole}`, {method:"DELETE"});
    if (job !== state.jobId) return;
    toast("该角色已切回本地模型");
    await loadDashboard();
  } catch (error) { toast(error.message, true); }
};
