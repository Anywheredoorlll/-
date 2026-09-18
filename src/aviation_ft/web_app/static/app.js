const state={jobs:[],jobId:null,dashboard:null,view:"overview",evidence:null,candidates:null};
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>new Intl.NumberFormat("zh-CN").format(n||0);
async function api(path,options={}){const res=await fetch(path,options);const type=res.headers.get("content-type")||"";const data=type.includes("json")?await res.json():null;if(!res.ok)throw new Error(data?.error||"请求失败");return data}
function toast(message,error=false){const el=$("#toast");el.textContent=message;el.style.background=error?"#c8443c":"#22344f";el.classList.add("show");clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove("show"),2600)}
function head(title,subtitle){return `<div class="page-head"><div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><span class="status-pill"><i></i>${esc(state.dashboard?.meta?.status||"等待任务")}</span></div>`}
function loading(){return '<div class="empty"><div><strong>正在加载</strong>同步任务数据…</div></div>'}
function syncProviderChrome(){const p=state.dashboard?.provider;const external=p?.mode==="external";const label=$("#provider-mode-label"),detail=$("#provider-mode-detail"),env=$("#provider-env");if(label)label.textContent=external?"外部 API 模式":"本地模型模式";if(detail)detail.textContent=external?(p.model||"OpenAI 兼容接口"):"仅监听 127.0.0.1";if(env)env.innerHTML='<i></i>'+(external?"外部 API":"本地模型")}
async function loadJobs(preferred){
 state.jobs=await api("/api/jobs");
 const select=$("#job-select");
 select.innerHTML=state.jobs.length?state.jobs.map(j=>`<option value="${j.job_id}">${esc(j.label||"未命名任务")} · ${j.job_id.slice(0,8)}</option>`).join(""):'<option value="">暂无任务</option>';
 const currentExists=state.jobs.some(job=>job.job_id===state.jobId);
 state.jobId=preferred||(currentExists?state.jobId:null)||state.jobs[0]?.job_id||null;
 if(state.jobId)select.value=state.jobId;
 $("#delete-job-button").disabled=!state.jobId;
 await loadDashboard();
}
async function loadDashboard(){if(!state.jobId){state.dashboard=null;renderEmpty();return}state.dashboard=await api(`/api/jobs/${state.jobId}`);syncProviderChrome();await render()}
async function render(){return await ({overview:renderOverview,upload:renderUpload,evidence:renderEvidence,review:renderReview,export:renderExport,provider:renderProvider}[state.view]||renderOverview)()}
function renderEmpty(){$("#content").innerHTML=head("任务工作台","民航回答微调数据集的构建、核验与交付")+'<div class="card empty"><div><strong>还没有数据集任务</strong>请点击左侧“新建任务”开始。</div></div>'}
function metric(tone,icon,label,value,detail){return `<div class="metric-card ${tone}"><span class="metric-icon">${icon}</span><div class="metric-copy"><small>${label}</small><strong>${fmt(value)}</strong><em>${detail}</em></div></div>`}
function renderOverview(){const d=state.dashboard,p=d.progress,q=d.quality,m=d.meta;let current=!p.parsed_pages?1:p.evidence<q.evidence_target?2:!q.final_ready?3:4;const labels=["上传文档","提取证据","生成与核验","下载数据包"];const steps=labels.map((x,i)=>`<div class="step ${i+1<current?"done":i+1===current?"current":""}"><b>${i+1}</b>${x}</div>`).join("");const docs=m.documents.slice(0,5).map(x=>`<tr><td>${esc(x.filename)}</td><td>PDF 文档</td><td><span class="badge green">已上传</span></td><td>${x.pages} 页</td><td>${new Date(x.uploaded_at).toLocaleString("zh-CN")}</td></tr>`).join("");$("#content").innerHTML=head("今日工作概览","专注于数据、模型与知识的智能研究平台")+`
<div class="overview-grid">
 <div>
  <section class="card card-pad">
   <div class="section-head"><strong>运行数据</strong><span>实时同步</span></div>
   <div class="metric-grid">
    ${metric("blue","PDF","已上传文档",p.documents,"份原始资料")}
    ${metric("cyan","文","原文证据",p.evidence,`目标 ${q.evidence_target}`)}
    ${metric("violet","问","候选问答",p.candidates,"自动生成与核验")}
    ${metric("orange","✓","审核通过",p.approved,`目标 ${q.approved_target}`)}
   </div>
   <div class="workflow">
    <div class="section-head"><strong>构建流程</strong><span>任务 ID · ${state.jobId.slice(0,8)}</span></div>
    <div class="steps">${steps}</div>
    <div class="next-action"><span>↗</span><div><small>建议下一步</small><strong>${esc(p.next_step)}</strong></div><button id="autopilot" class="primary" ${!p.documents||d.automation_running?"disabled":""}>${d.automation_running?"构建中…":"开始 / 继续构建"}</button></div>
   </div>
  </section>
  <section class="card card-pad" style="margin-top:12px">
   <div class="section-head"><strong>最近文档</strong><button class="secondary" data-go="upload">查看全部</button></div>
   ${docs?`<div class="table-wrap"><table><thead><tr><th>文档名称</th><th>类型</th><th>状态</th><th>页数</th><th>更新时间</th></tr></thead><tbody>${docs}</tbody></table></div>`:'<div class="empty"><div><strong>暂无文档</strong>前往数据中心上传 PDF。</div></div>'}
  </section>
 </div>
 <aside class="card assistant">
  <div class="assistant-head"><span class="assistant-avatar">✦</span><div><strong>AI 助手</strong><small>你的智能研究伙伴</small></div><i class="online"></i></div>
  <div class="assistant-focus"><small>建议优先处理</small><strong>${esc(p.next_step)}</strong></div>
  <div class="assistant-item"><span>!</span><div><strong>疑难原文 ${d.human_queue_count} 项</strong><small>只需处理影响结构目标的异常</small></div></div>
  <div class="assistant-item green"><span>✓</span><div><strong>自动核验通过 ${q.approved_by_review_mode.automated} 条</strong><small>审核方式已分别留痕</small></div></div>
  <div class="assistant-item orange"><span>◆</span><div><strong>质量门槛 ${q.issues.length} 项待处理</strong><small>全部完成后可导出正式包</small></div></div>
 </aside>
</div>`;
$("#autopilot")?.addEventListener("click",startAutopilot);$$("[data-go]").forEach(b=>b.onclick=()=>switchView(b.dataset.go))}
async function startAutopilot(){try{await api(`/api/jobs/${state.jobId}/autopilot`,{method:"POST"});toast("自动构建已启动");await loadDashboard()}catch(e){toast(e.message,true)}}
function renderUpload(){
 const m=state.dashboard.meta;
 const txt=state.dashboard.semantic_txt||{};
 const locked=state.dashboard.progress.parsed_pages>0||state.dashboard.progress.evidence>0;
 const rows=m.documents.map(d=>`<tr><td>${esc(d.filename)}</td><td>${d.pages}</td><td>${(d.size_bytes/1048576).toFixed(1)} MB</td><td><code>${d.sha256.slice(0,16)}…</code></td><td>${locked?"已锁定":`<button class="danger remove-doc" data-file="${esc(d.filename)}">移除</button>`}</td></tr>`).join("");
 $("#content").innerHTML=head("数据中心","上传 PDF、重建条款证据，并保留可下载的整体文本分支")+`
<section class="card card-pad">
 <div class="section-head"><strong>上传原始资料</strong><span>单文件 200 MB · 每任务 600 MB</span></div>
 <div class="upload-zone"><div><strong>${locked?"文档集合已锁定":"拖放或选择 PDF 文件"}</strong><p>${locked?"解析已开始，如需增加文件请新建任务。":"支持一次选择多个文件；解析时保留原始坐标并重建条款。"}</p><input id="pdf-files" type="file" accept=".pdf,application/pdf" multiple ${locked?"disabled":""}></div></div>
 <div class="button-row"><button id="upload-files" class="primary" ${locked?"disabled":""}>上传所选文件</button><button id="parse-files" class="secondary" ${locked||!m.documents.length?"disabled":""}>解析并重建条款</button></div>
</section>
<section class="card card-pad" style="margin-top:12px">
 <div class="section-head"><strong>已上传文档</strong><span>${m.documents.length} 份</span></div>
 ${rows?`<div class="table-wrap"><table><thead><tr><th>文件名</th><th>页数</th><th>大小</th><th>SHA-256</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="empty"><div><strong>暂无文件</strong>选择 PDF 后上传。</div></div>'}
</section>
<section class="card card-pad txt-branch" style="margin-top:12px">
 <div class="section-head"><strong>整体 TXT 分支</strong><span class="badge ${txt.ready?"green":""}">${txt.ready?"已生成":"等待解析"}</span></div>
 <p>按文档和条款顺序合并完整文本；同一条跨页断句仅在此 TXT 分支中连接，不改变逐条证据块及其坐标审计信息。</p>
 <div class="button-row">
  <button id="build-semantic-txt" class="secondary" ${!locked?"disabled":""}>${txt.ready?"重新生成整体 TXT":"生成整体 TXT"}</button>
  ${txt.ready?`<a class="secondary download-link" href="/api/jobs/${state.jobId}/semantic-txt">下载整体 TXT · ${(txt.size_bytes/1024).toFixed(1)} KB</a>`:""}
 </div>
</section>`;
 $("#upload-files").onclick=uploadFiles;
 $("#parse-files").onclick=parseFiles;
 $("#build-semantic-txt")?.addEventListener("click",buildSemanticTxt);
 $$(".remove-doc").forEach(b=>b.onclick=()=>removeDoc(b.dataset.file));
}
async function uploadFiles(){const files=$("#pdf-files").files;if(!files.length)return toast("请选择 PDF",true);const fd=new FormData();[...files].forEach(f=>fd.append("files",f));try{toast("正在上传…");await api(`/api/jobs/${state.jobId}/upload`,{method:"POST",body:fd});toast("上传完成");await loadDashboard()}catch(e){toast(e.message,true)}}
async function parseFiles(){try{toast("正在解析 PDF…");await api(`/api/jobs/${state.jobId}/parse`,{method:"POST"});toast("解析完成");await loadDashboard()}catch(e){toast(e.message,true)}}
async function buildSemanticTxt(){try{toast("正在合并条款文本…");const result=await api(`/api/jobs/${state.jobId}/semantic-txt`,{method:"POST"});toast(`整体 TXT 已生成 · ${fmt(result.semantic_units)} 个语义单元`);await loadDashboard()}catch(e){toast(e.message,true)}}
async function removeDoc(file){if(!confirm(`确认移除 ${file} 的服务器副本？`))return;try{await api(`/api/jobs/${state.jobId}/remove-document`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({filename:file})});toast("文件已移除");await loadDashboard()}catch(e){toast(e.message,true)}}
async function renderEvidence(){if(!state.evidence)state.evidence=await api(`/api/jobs/${state.jobId}/evidence`);const e=state.evidence;$("#content").innerHTML=head("原文证据","定位异常页面、核对原文并管理可追溯证据")+`
<div class="metric-grid" style="margin-bottom:12px">${metric("blue","!","疑难片段",e.queue.length,"自动流程跳过项")}${metric("cyan","OCR","OCR / 少文本",e.queue.filter(x=>String(x.reason).includes("OCR")).length,"优先人工核对")}${metric("violet","文","已确认证据",e.units.length,"可追溯引用")}${metric("orange","◎","结构目标",state.dashboard.quality.evidence_target,"证据单元")}</div>
<div class="tabs" id="evidence-tabs"><button class="tab active" data-tab="issues">异常概览</button><button class="tab" data-tab="page">逐页核对</button><button class="tab" data-tab="library">证据库</button></div><section id="evidence-panel"></section>`;$$("[data-tab]").forEach(b=>b.onclick=()=>{$$("[data-tab]").forEach(x=>x.classList.toggle("active",x===b));renderEvidenceTab(b.dataset.tab)});renderEvidenceTab("issues")}
function renderEvidenceTab(tab){if(tab==="issues")renderIssues();else if(tab==="page")renderPageReview();else renderLibrary()}
function renderIssues(){const e=state.evidence;const rows=e.queue.map(x=>`<tr><td>${x.pdf_page}</td><td>${esc(x.reason)}</td><td><code>${esc(x.block_id)}</code></td></tr>`).join("");$("#evidence-panel").innerHTML=`<section class="card card-pad"><div class="section-head"><strong>需要人工判断的页面</strong><span>${e.frozen?"分区已冻结，无需逐条处理":"优先处理 OCR 与少文本页"}</span></div>${rows?`<div class="table-wrap"><table><thead><tr><th>PDF 页码</th><th>异常原因</th><th>原文定位</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="empty"><div><strong>没有原文异常</strong>可以继续检查证据库。</div></div>'}</section>`}
function renderPageReview(){const e=state.evidence;if(!e.pages.length){$("#evidence-panel").innerHTML='<div class="card empty"><div><strong>暂无解析页面</strong>请先上传并解析 PDF。</div></div>';return}const docs=[...new Map(e.pages.map(p=>[p.doc_id,p.filename])).entries()];$("#evidence-panel").innerHTML=`<section class="card card-pad"><div class="toolbar"><label>文档<select class="field" id="page-doc">${docs.map(([id,name])=>`<option value="${id}">${esc(name)}</option>`).join("")}</select></label><label>PDF 页码<select class="field" id="page-number"></select></label></div><div id="page-workspace">${loading()}</div></section>`;$("#page-doc").onchange=fillPages;fillPages()}
function fillPages(){const doc=$("#page-doc").value,pages=state.evidence.pages.filter(p=>p.doc_id===doc);$("#page-number").innerHTML=pages.map(p=>`<option value="${p.pdf_page}">${p.pdf_page}${p.ocr_used?" · OCR":""}</option>`).join("");$("#page-number").onchange=loadPage;loadPage()}
async function loadPage(){
 const doc=$("#page-doc").value,page=$("#page-number").value;
 const data=await api(`/api/jobs/${state.jobId}/page?doc_id=${encodeURIComponent(doc)}&page=${page}`);
 const blocks=data.semantic_blocks||[];
 const quality=b=>b.quality_issues||[];
 const unsafe=b=>b.kind==="heading"||quality(b).length>0;
 let selected=blocks.findIndex(b=>!unsafe(b));
 if(selected<0)selected=0;
 const warning=b=>quality(b)[0]||(b.kind==="heading"?"标题不作为问答证据":"已按版面顺序重组，可作为连续证据");
 const badge=b=>`${b.kind==="table"?"表格":b.kind==="heading"?"标题":b.kind==="list_item"?"列表项":"条款"} · 第 ${b.pdf_page||page}${b.end_pdf_page>b.pdf_page?"–"+b.end_pdf_page:""} 页`;
 $("#page-workspace").innerHTML=`<div class="split-view"><iframe class="pdf-frame" src="/api/jobs/${state.jobId}/pdf/${doc}#page=${page}&view=FitH"></iframe><div><div class="semantic-summary"><strong>语义段落 ${blocks.length} 个</strong><span>由 ${(data.blocks||[]).length} 个原始文本块重组，原始定位信息已保留</span></div><div class="text-blocks semantic-blocks">${blocks.map((b,i)=>`<div class="text-block semantic-block ${i===selected?"selected":""} ${unsafe(b)?"needs-review":""}" data-index="${i}"><div class="semantic-meta"><code>${esc(b.semantic_id)}</code><span class="badge">${badge(b)}</span></div><div class="semantic-text">${esc(b.text)}</div><small>${esc(warning(b))}</small></div>`).join("")||'<div class="empty">本页没有识别文本</div>'}</div>${blocks.length?`<form id="evidence-form"><div id="semantic-state" class="semantic-state"></div><div class="form-grid"><label>主题<input class="field" name="topic" required></label><label>章节组<input class="field" name="clause_group" required></label><label>数据分区<select class="field" name="split"><option>train</option><option>validation</option><option>test</option></select></label></div><label class="stack-label" style="margin-top:10px">核对人 ID<input class="field" name="reviewer" required placeholder="例如 reviewer-01"></label><label class="stack-label" style="margin-top:10px">连续证据预览<textarea name="quote"></textarea></label><button id="save-semantic-block" class="primary" style="margin-top:8px">保存当前语义段落</button></form>`:""}</div></div>`;
 function selectBlock(index){
  selected=index;
  const block=blocks[selected];
  $$(".semantic-block").forEach((item,i)=>item.classList.toggle("selected",i===selected));
  const invalid=unsafe(block);
  $("#evidence-form textarea").value=block.text;
  $("#semantic-state").className=`semantic-state ${invalid?"warning":"ready"}`;
  $("#semantic-state").textContent=warning(block);
  $("#save-semantic-block").disabled=invalid;
 }
 $$(".semantic-block").forEach(item=>item.onclick=()=>selectBlock(+item.dataset.index));
 if(blocks.length)selectBlock(selected);
 $("#evidence-form")?.addEventListener("submit",async ev=>{
  ev.preventDefault();
  const block=blocks[selected],fd=new FormData(ev.target);
  if(unsafe(block))return toast(warning(block),true);
  const item={block_id:block.block_id,start_block_id:block.start_block_id,end_block_id:block.end_block_id,pdf_page:block.pdf_page||Number(page),topic:fd.get("topic"),clause_group:fd.get("clause_group"),split:fd.get("split"),quote:fd.get("quote")};
  try{await api(`/api/jobs/${state.jobId}/evidence/batch`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({items:[item],reviewer:fd.get("reviewer")})});toast("连续证据已保存");state.evidence=null;await loadDashboard()}catch(err){toast(err.message,true)}
 });
}
function renderLibrary(){const e=state.evidence;const rows=e.units.map(x=>`<tr><td><code>${x.evidence_unit_id}</code></td><td>${esc(x.filename)}</td><td>${x.pdf_page}</td><td>${esc(x.topic)}</td><td>${esc(x.clause_group)}</td><td><span class="badge">${x.split}</span></td></tr>`).join("");$("#evidence-panel").innerHTML=`<section class="card card-pad"><div class="section-head"><strong>已确认证据</strong><button id="freeze-evidence" class="primary" ${e.frozen||!e.units.length?"disabled":""}>${e.frozen?"分区已冻结":"冻结证据分区"}</button></div>${rows?`<div class="table-wrap"><table><thead><tr><th>证据 ID</th><th>文档</th><th>页码</th><th>主题</th><th>章节组</th><th>分区</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="empty"><div><strong>尚无证据</strong>在逐页核对中保存原文块。</div></div>'}</section>`;$("#freeze-evidence")?.addEventListener("click",freezeEvidence)}
async function freezeEvidence(){if(!confirm("冻结后不能再修改证据，确认继续？"))return;try{await api(`/api/jobs/${state.jobId}/evidence/freeze`,{method:"POST"});toast("证据分区已冻结");state.evidence=null;await loadDashboard()}catch(e){toast(e.message,true)}}
async function renderReview(){if(!state.candidates)state.candidates=await api(`/api/jobs/${state.jobId}/candidates`);const items=state.candidates.items;const rows=items.slice(0,200).map(x=>`<tr><td><input type="checkbox" class="candidate-check" value="${x.sample_id}" ${x.errors.length?"disabled":""}></td><td><code>${x.sample_id}</code></td><td><span class="badge">${x.split}</span></td><td>${esc(x.question)}</td><td>${esc(x.answer)}</td><td>${x.errors.length?`<span class="badge red">需修正</span>`:`<span class="badge orange">待审核</span>`}</td></tr>`).join("");$("#content").innerHTML=head("问答审核","集中核对模型候选及其引用证据")+`<section class="card card-pad"><div class="section-head"><strong>待审核候选</strong><span>显示 ${Math.min(items.length,200)} / ${items.length}</span></div><div class="toolbar"><label>审核结论<select id="review-decision" class="field"><option value="approved">通过</option><option value="pending_expert_review">待专家核对</option><option value="rejected">拒绝</option></select></label><label>审核人 ID<input id="reviewer" class="field"></label><label>备注<input id="review-comment" class="field"></label><button id="review-submit" class="primary">提交所选</button></div>${rows?`<div class="table-wrap"><table><thead><tr><th></th><th>样本 ID</th><th>分区</th><th>问题</th><th>答案</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="empty"><div><strong>没有待审核候选</strong>自动构建会在生成后将异常项放到这里。</div></div>'}</section>`;$("#review-submit").onclick=submitReview}
async function submitReview(){const ids=$$(".candidate-check:checked").map(x=>x.value);try{await api(`/api/jobs/${state.jobId}/review-batch`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sample_ids:ids,reviewer:$("#reviewer").value,decision:$("#review-decision").value,comment:$("#review-comment").value})});toast("审核记录已提交");state.candidates=null;await loadDashboard()}catch(e){toast(e.message,true)}}
function renderExport(){
 const d=state.dashboard,q=d.quality,bundles=d.exports||{};
 const issues=q.issues.map(x=>`<li>${esc(x)}</li>`).join("");
 const link=(kind,label,primary=false)=>bundles[kind]?.ready?`<a class="${primary?"primary":"secondary"} download-link" style="display:block;margin-top:8px;text-align:center;text-decoration:none;padding:10px" href="/api/jobs/${state.jobId}/exports/${kind}">${label} · ${(bundles[kind].size_bytes/1024).toFixed(1)} KB</a>`:"";
 $("#content").innerHTML=head("结果交付","默认下载训练文件，需要核查来源时再下载完整追溯包")+`
 <div class="overview-grid"><section class="card card-pad">
  <div class="section-head"><strong>质量门槛</strong><span class="badge ${q.final_ready?"green":"orange"}">${q.final_ready?"结构已达标":"等待完成"}</span></div>
  <ul class="quality-list">${issues||"<li>所有结构门槛均已通过；语义质量仍需独立抽样验收。</li>"}</ul>
 </section><aside class="card card-pad">
  <div class="section-head"><strong>训练包 · 默认下载</strong></div>
  <p style="color:#8592a5;font-size:12px;line-height:1.7">仅包含训练集、验证集、测试集和简短说明。预览包不可训练；正式包仍需独立验收内容。</p>
  <button class="primary export-button" data-final="true" style="width:100%" ${q.final_ready?"":"disabled"}>生成正式包</button>
  ${link("final","下载训练包",true)}
  <details style="margin-top:18px"><summary>完整追溯包（可选）</summary>
   <p style="color:#8592a5;font-size:12px;line-height:1.7">包含同一份训练数据，以及证据原文、来源信息、审核记录、生成配置和运行日志。生成正式包时同步生成。</p>
   ${link("final-trace","下载完整追溯包")||'<p>生成正式包后可下载。</p>'}
  </details>
  <details style="margin-top:18px"><summary>预览文件</summary>
   <button class="secondary export-button" data-final="false" style="width:100%;margin-top:8px">生成预览包</button>
   ${link("preview","下载精简预览包")}${link("preview-trace","下载预览追溯包")}
  </details>
  ${d.semantic_txt?.ready?`<a class="secondary download-link" style="display:block;margin-top:18px;text-align:center;text-decoration:none;padding:10px" href="/api/jobs/${state.jobId}/semantic-txt">单独下载 TXT</a>`:""}
 </aside></div>`;
 $$(".export-button").forEach(b=>b.onclick=()=>exportData(b.dataset.final==="true"));
}
async function exportData(final){try{toast("正在生成数据包…");const x=await api(`/api/jobs/${state.jobId}/export`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({final})});toast(`已生成 ${x.filename} · ${x.sha256.slice(0,12)}…`);await loadDashboard()}catch(e){toast(e.message,true)}}
function renderProvider(){const p=state.dashboard.provider||{};const presets=state.dashboard.provider_presets||[];const selected=presets.some(x=>x.id===p.preset_id)?p.preset_id:(presets[0]?.id||"");const providerOptions=presets.map(x=>`<option value="${esc(x.id)}" ${x.id===selected?"selected":""}>${esc(x.name)}</option>`).join("");const selectedPreset=presets.find(x=>x.id===selected);const modelOptions=(selectedPreset?.models||[]).map(x=>`<option value="${esc(x)}" ${x===p.model?"selected":""}>${esc(x)}</option>`).join("");const external=p.mode==="external";$("#content").innerHTML=head("模型 API","选择国内模型服务商，只需填写 API Key 即可接入")+`
<div class="provider-mode-grid">
 <section class="card card-pad">
  <div class="section-head"><strong>当前 Provider</strong><span class="badge ${external?"orange":"green"}">${external?"外部 API":"本地模型"}</span></div>
  <div class="provider-current">
   <span class="provider-logo">${external?"API":"GPU"}</span>
   <div><strong>${external?esc(p.model):"aviation-generator"}</strong><small>${external?esc(p.base_url):"http://127.0.0.1:8000/v1"}</small></div>
  </div>
  <p class="provider-note">设置只对当前任务生效。系统不会在本地失败后自动外发；只有切换为外部 API 后，自动构建才会发送证据文本。</p>
 </section>
 <aside class="card card-pad privacy-card">
  <div class="section-head"><strong>数据边界</strong><span>任务级授权</span></div>
  <ul><li>发送内容：证据文本、问题、答案和核验提示</li><li>不发送：原始 PDF 文件、其他任务数据</li><li>API Key：服务端 0600 保存，不回显、不导出</li></ul>
 </aside>
</div>
<section class="card card-pad" style="margin-top:12px">
 <div class="section-head"><strong>OpenAI 兼容接口</strong><span>${p.configured?"已保存 · "+esc(p.api_key_masked):"尚未配置"}</span></div>
 <form id="provider-form">
  <div class="provider-form-grid">
   <label>模型服务商<select class="field" id="provider-preset" name="preset_id">${providerOptions}</select></label>
   <label>调用模型<select class="field" id="provider-model" name="model">${modelOptions}</select></label>
   <label>API Key<input class="field" name="api_key" type="password" autocomplete="new-password" placeholder="${p.api_key_masked?"留空则沿用 "+esc(p.api_key_masked):"粘贴服务商 API Key"}"></label>
   <input name="base_url" type="hidden" value="">
  </div>
  <label class="consent-row"><input type="checkbox" name="privacy_acknowledged" ${p.privacy_acknowledged?"checked":""}><span>我确认允许将当前任务的证据文本、问题和答案发送到上述外部 API 服务。</span></label>
  <div class="provider-actions"><button class="primary" type="submit">保存并启用外部 API</button><button class="secondary" type="button" id="test-provider" ${p.configured?"":"disabled"}>测试连接</button><button class="secondary" type="button" id="use-local">切回本地模型</button></div>
 </form>
</section>`;
$("#provider-preset").onchange=e=>{const preset=presets.find(x=>x.id===e.target.value);$("#provider-model").innerHTML=(preset?.models||[]).map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join("")};$("#provider-form").addEventListener("submit",saveProvider);$("#test-provider").onclick=testProvider;$("#use-local").onclick=useLocalProvider}
async function saveProvider(event){event.preventDefault();const fd=new FormData(event.target);const payload={preset_id:fd.get("preset_id"),base_url:"",model:fd.get("model"),api_key:fd.get("api_key"),privacy_acknowledged:fd.get("privacy_acknowledged")==="on"};try{await api(`/api/jobs/${state.jobId}/provider`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});toast("外部模型已保存并启用");await loadDashboard()}catch(e){toast(e.message,true)}}
async function testProvider(){try{toast("正在测试模型 API…");const result=await api(`/api/jobs/${state.jobId}/provider/test`,{method:"POST"});toast(`连接成功：${result.model}`)}catch(e){toast(e.message,true)}}
async function useLocalProvider(){try{await api(`/api/jobs/${state.jobId}/provider`,{method:"DELETE"});toast("已切回本地模型");await loadDashboard()}catch(e){toast(e.message,true)}}

async function switchView(view){state.view=view;state.evidence=null;state.candidates=null;$$(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.view===view));$("#content").innerHTML=loading();try{await render()}catch(e){toast(e.message,true);$("#content").innerHTML=`<div class="empty"><div><strong>页面加载失败</strong>${esc(e.message)}</div></div>`}}
$("#main-nav").addEventListener("click",e=>{const b=e.target.closest("[data-view]");if(b)switchView(b.dataset.view)});
$("#job-select").addEventListener("change",async e=>{state.jobId=e.target.value;state.evidence=null;state.candidates=null;await loadDashboard()});
$("#new-job-button").onclick=()=>$("#job-dialog").showModal();
$("#create-job").addEventListener("click",async e=>{e.preventDefault();const label=$("#job-label").value.trim();if(!label)return;try{const x=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({label})});$("#job-dialog").close();$("#job-label").value="";toast("任务已创建");await loadJobs(x.job_id)}catch(err){toast(err.message,true)}});
$("#delete-job-button").onclick=()=>{
 if(!state.jobId)return;
 $("#delete-job-id").textContent=state.jobId;
 $("#delete-job-confirmation").value="";
 $("#delete-job-dialog").showModal();
};
$("#confirm-delete-job").onclick=async()=>{
 const confirmation=$("#delete-job-confirmation").value.trim();
 if(confirmation!==state.jobId)return toast("请输入上方完整任务 ID",true);
 const button=$("#confirm-delete-job");
 button.disabled=true;
 try{
  await api(`/api/jobs/${state.jobId}`,{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirmation})});
  $("#delete-job-dialog").close();
  state.jobId=null;state.dashboard=null;state.evidence=null;state.candidates=null;
  toast("任务已永久删除");
  await loadJobs();
 }catch(e){toast(e.message,true)}
 finally{button.disabled=false}
};
$("#refresh-button").onclick=async()=>{state.evidence=null;state.candidates=null;await loadDashboard();toast("数据已刷新")};
$("#global-search").addEventListener("keydown",e=>{if(e.key==="Enter"){const q=e.target.value.toLowerCase();const match=state.jobs.find(j=>(j.label||"").toLowerCase().includes(q)||j.job_id.includes(q));if(match){state.jobId=match.job_id;$("#job-select").value=match.job_id;loadDashboard()}else toast("没有匹配的任务",true)}});
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){e.preventDefault();$("#global-search").focus()}});
loadJobs().catch(e=>toast(e.message,true));
