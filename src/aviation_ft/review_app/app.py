from __future__ import annotations

import importlib
import io
import html
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import pymupdf as fitz
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aviation_ft import automation, core, generation, workflow

# Streamlit reruns this script without re-importing already loaded modules.
core = importlib.reload(core)
generation = importlib.reload(generation)
workflow = importlib.reload(workflow)
automation = importlib.reload(automation)

st.set_page_config(page_title="民航数据集工作台", page_icon="✦", layout="wide")
style = (Path(__file__).with_name("theme.css")).read_text(encoding="utf-8")
st.markdown(f"<style>{style}</style>", unsafe_allow_html=True)


def hero(status: str) -> None:
    st.markdown(
        '<div class="workspace-heading">'
        '<div><span class="workspace-kicker">AVIATION DATASET OPERATIONS</span>'
        '<h1>任务工作台</h1>'
        '<p>民航回答微调数据集的构建、核验与交付</p></div>'
        '<div class="workspace-status"><i></i>'
        f'<span>{html.escape(status)}</span></div>'
        '</div>',
        unsafe_allow_html=True,
    )


def action(fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        st.success("操作完成")
        return result
    except Exception as exc:
        st.error(str(exc))
        return None


jobs = core.list_jobs()
with st.sidebar:
    st.markdown(
        '<div class="aviation-brand"><span class="aviation-brand-mark">A</span>'
        '<div><strong>AeroForge</strong><small>DATASET OPERATIONS</small></div></div>',
        unsafe_allow_html=True,
    )
    st.header("任务")
    label = st.text_input("新任务名称", placeholder="例如：民航标准资料集")
    if st.button("创建任务", type="primary"):
        job_id = action(core.create_job, label)
        if job_id:
            st.session_state.job_id = job_id
            st.rerun()
    choices = [j["job_id"] for j in jobs]
    current = st.session_state.get("job_id")
    if choices:
        idx = choices.index(current) if current in choices else 0
        selected = st.selectbox("选择已有任务", choices, index=idx, format_func=lambda x: next((f"{j['label'] or '未命名'} · {x[:8]} · {j['status']}" for j in jobs if j["job_id"] == x), x))
        st.session_state.job_id = selected
        with st.expander("删除当前任务"):
            st.warning("将永久删除上传文档、证据、审核记录和导出文件。")
            delete_confirmation = st.text_input(
                "输入完整任务 ID 确认",
                key=f"delete-confirmation-{selected}",
            )
            if st.button(
                "永久删除当前任务",
                disabled=delete_confirmation != selected,
                key=f"delete-job-{selected}",
            ):
                session = f"aviation-auto-{selected[:8]}"
                running = subprocess.run(
                    ["screen", "-ls"], capture_output=True, text=True, check=False,
                )
                if f".{session}" in running.stdout:
                    st.error("自动构建正在运行，不能删除任务。")
                else:
                    deleted = action(core.delete_job, selected, delete_confirmation)
                    if deleted:
                        st.session_state.pop("job_id", None)
                        st.rerun()
    else:
        st.session_state.pop("job_id", None)
    st.divider()
    st.write("生成模型固定位置")
    st.code(core.settings()["generator_model_dir"])
    st.info("本机模型完成生成与双重核验；不能可靠判断的样本交给人工。")
    st.warning("仅通过 SSH 隧道访问；不得直接暴露到公网。")

job_id = st.session_state.get("job_id")
if not job_id:
    hero("等待创建任务")
    st.markdown('<div class="aviation-card"><h3>从一份新任务开始</h3><p>在左侧输入任务名称并创建；可以分批上传任意名称的 PDF；本任务上传完成后再运行自动流程。原始 PDF 仅暂存于服务器任务目录。</p></div>', unsafe_allow_html=True)
    st.stop()
meta = core.load_job(job_id)
progress = workflow.task_progress(job_id)
quality = core.quality_report(job_id)
hero(f"任务 {job_id[:8]} · {meta['status']}")
current_step = 1 if not progress["parsed_pages"] else 2 if progress["evidence"] < quality["evidence_target"] else 3 if not quality["final_ready"] else 4
steps = ["上传文档", "提取证据", "生成与核验", "下载数据包"]
step_html = ''.join(
    f'<div class="aviation-step {"done" if i < current_step else "current" if i == current_step else ""}">'
    f'<strong>{i}</strong><span>{label}</span></div>'
    for i, label in enumerate(steps, 1)
)
auto_session = f"aviation-auto-{job_id[:8]}"
screen_list = subprocess.run(["screen", "-ls"], capture_output=True, text=True, check=False)
auto_running = f".{auto_session}" in screen_list.stdout
auto_log = core.read_jsonl(core.job_dir(job_id) / "automation/progress.jsonl")
latest = auto_log[-1] if auto_log else None
evidence_queue_count = len(core.read_jsonl(core.job_dir(job_id) / "evidence/human_queue.jsonl"))

metric_cards = [
    ("blue", "PDF", "已上传文档", progress["documents"], "份原始资料"),
    ("cyan", "文", "原文证据", progress["evidence"], f"目标 {quality['evidence_target']}"),
    ("violet", "问", "候选问答", progress["candidates"], "等待生成与核验"),
    ("orange", "✓", "审核通过", progress["approved"], f"目标 {quality['approved_target']}"),
]
metric_html = "".join(
    f'<div class="dashboard-metric {tone}"><span class="metric-icon">{icon}</span>'
    f'<div><small>{label}</small><strong>{value}</strong><em>{detail}</em></div></div>'
    for tone, icon, label, value, detail in metric_cards
)

dashboard_main, dashboard_side = st.columns([3.2, 1], gap="large")
with dashboard_main:
    with st.container(border=True):
        st.markdown(
            '<div class="panel-heading"><div><strong>构建概览</strong><span>实时任务数据</span></div>'
            f'<small>任务 ID · {job_id[:8]}</small></div>',
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="dashboard-metrics">{metric_html}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="panel-heading workflow-heading"><div><strong>构建流程</strong>'
            '<span>按阶段完成数据准备</span></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="aviation-steps">{step_html}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="aviation-next"><span class="next-icon">↗</span><div><small>建议下一步</small>'
            f'<strong>{html.escape(progress["next_step"])}</strong></div></div>',
            unsafe_allow_html=True,
        )
        st.caption("确认本任务文件齐全后再启动；解析开始后文档集合将锁定。")
        if auto_running:
            st.info(f"自动构建正在后台运行 · {auto_session}")
        if st.button(
            "上传完成，开始 / 继续自动构建",
            type="primary",
            disabled=progress["documents"] == 0 or auto_running,
            width="stretch",
        ):
            try:
                subprocess.run([
                    "screen", "-dmS", auto_session,
                    sys.executable, "-m", "aviation_ft.cli",
                    "autopilot", job_id, "--until-ready",
                ], check=True)
                st.success("自动任务已启动；刷新页面查看进度。")
                st.rerun()
            except Exception as exc:
                st.error(f"启动自动任务失败：{exc}")
        if latest:
            st.caption(f"最近状态：{latest['status']} · {latest['at']}")
            if latest["status"] in {"needs_human", "needs_upload", "failed"}:
                st.warning(latest.get("summary", {}).get("reason", "自动流程需要人工处理，请查看下方工作区。"))

with dashboard_side:
    with st.container(border=True):
        st.markdown(
            '<div class="assistant-head"><span class="assistant-avatar">✦</span>'
            '<div><strong>构建助手</strong><small>任务状态与建议</small></div>'
            '<i></i></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="assistant-focus"><small>当前建议</small>'
            f'<strong>{html.escape(progress["next_step"])}</strong></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="assistant-item"><span class="assistant-item-icon blue">!</span>'
            f'<div><strong>疑难原文 {evidence_queue_count} 项</strong>'
            '<small>只需处理影响证据目标的异常</small></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="assistant-item"><span class="assistant-item-icon green">✓</span>'
            f'<div><strong>自动核验通过 {quality["approved_by_review_mode"]["automated"]} 条</strong>'
            '<small>人工通过与批量通过单独留痕</small></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="assistant-item"><span class="assistant-item-icon orange">◆</span>'
            f'<div><strong>质量门槛 {len(quality["issues"])} 项待处理</strong>'
            '<small>结构达标后才可导出正式包</small></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="assistant-foot"><i></i><span>任务状态已同步</span></div>',
            unsafe_allow_html=True,
        )

if progress["approved"] >= quality["approved_target"] and not quality["final_ready"]:
    st.error("正式数据包未达标：" + "；".join(quality["issues"][:5]))
if meta["status"] == "preview_exported":
    preview_path = core.job_dir(job_id) / "exports/aviation-sft-preview.zip"
    preview_meta = core.read_json(preview_path.with_suffix(".sha256.json"), {})
    candidates_path = core.job_dir(job_id) / "candidates/records.jsonl"
    evidence_path = core.job_dir(job_id) / "evidence/units.jsonl"
    if preview_path.exists() and (
        preview_meta.get("candidates_sha256") != (core.sha256_file(candidates_path) if candidates_path.exists() else None)
        or preview_meta.get("evidence_sha256") != (core.sha256_file(evidence_path) if evidence_path.exists() else None)
    ):
        st.warning("现有预览 ZIP 已过期，不包含最新审核结果；请在导出页重新生成。预览包不能用于训练。")

t_upload, t_evidence, t_candidates, t_export = st.tabs(["① 上传", "② 原文异常/手动", "③ 问答异常/手动", "④ 结果下载与清理"])

with t_upload:
    st.subheader("分批上传 PDF")
    st.write("文件名和数量不限；可先上传一部分，之后继续补传。单文件上限 200 MB、每任务总量 600 MB。确认上传完成后再开始构建。")
    uploads_locked = (core.job_dir(job_id) / "parsed/pages.jsonl").exists() or (core.job_dir(job_id) / "evidence/units.jsonl").exists()
    if uploads_locked:
        st.info("此任务已开始解析，文档集合已锁定；如需增加 PDF，请新建任务。")
    uploaded = st.file_uploader("选择 PDF", type="pdf", accept_multiple_files=True, disabled=uploads_locked)
    if st.button("上传所选文件", disabled=not uploaded or uploads_locked):
        for file in uploaded or []:
            result = action(core.upload_pdf, job_id, file.name, file)
            if result:
                st.write(f"{result['filename']} · {result['pages']} 页 · SHA-256 `{result['sha256']}`")
        st.rerun()
    docs = core.load_job(job_id)["documents"]
    if docs:
        st.dataframe([{k: d[k] for k in ("filename", "pages", "size_bytes", "sha256")} for d in docs], hide_index=True, width="stretch")
        if not uploads_locked:
            with st.expander("管理已上传文件"):
                remove_file = st.selectbox("选择要移除的 PDF", [d["filename"] for d in docs])
                acknowledged = st.checkbox("确认移除该文件的服务器副本，本地原文件不受影响")
                if st.button("移除选中的 PDF", disabled=not acknowledged):
                    action(core.remove_unparsed_pdf, job_id, remove_file)
                    st.rerun()
        if st.button("上传完成，仅解析 PDF", disabled=uploads_locked):
            result = action(core.parse_job, job_id)
            if result:
                st.json(result)
    report = core.read_json(core.job_dir(job_id) / "parsed/report.json")
    if report:
        st.info(f"已解析 {report['pages']} 页、{report['blocks']} 个文本块；OCR 处理 {report.get('ocr_pages', 0)} 页；仍需人工检查的少文本页 {report['possible_scanned_pages']} 页。")
        st.subheader("整体 TXT 分支")
        st.caption("按文档、证据块和 PDF 页码顺序拼接所有识别文本；不修改可审计的原始坐标数据。")
        txt_path = core.job_dir(job_id) / "parsed/semantic_corpus.txt"
        if st.button("重新生成整体 TXT", width="stretch"):
            result = action(core.build_semantic_txt, job_id)
            if result:
                st.rerun()
        txt_status = core.sources.artifact_status(job_id, txt_path, core.read_json(txt_path.parent / "txt_report.json", {}))
        if txt_status["stale"]:
            st.warning("TXT 来源已过期，请主动重新生成。")
        if txt_status["ready"]:
            st.download_button(
                "下载整体 TXT",
                data=txt_path.read_bytes(),
                file_name=f"{job_id}-semantic-corpus.txt",
                mime="text/plain; charset=utf-8",
                width="stretch",
                key=f"download-semantic-txt-{job_id}",
            )

with t_evidence:
    evidence_human_queue = core.read_jsonl(core.job_dir(job_id) / "evidence/human_queue.jsonl")
    pages = core.pages_for(job_id)
    docs = core.load_job(job_id)["documents"] if pages else []
    frozen_path = core.job_dir(job_id) / "evidence/splits.json"
    frozen_evidence = frozen_path.exists()
    existing_units = core.evidence_for(job_id) if pages else []
    ocr_issue_count = sum("OCR" in item.get("reason", "") for item in evidence_human_queue)

    st.markdown('<div class="aviation-section-kicker">EVIDENCE OPERATIONS</div>', unsafe_allow_html=True)
    st.subheader("原文证据工作台")
    st.caption("先看异常是否影响证据数量，再批量整理；只有 OCR、表格或跨行条款需要进入逐页核对。")

    queue_metric, ocr_metric, saved_metric, target_metric = st.columns(4)
    queue_metric.metric("疑难片段", len(evidence_human_queue))
    ocr_metric.metric("其中 OCR / 少文本", ocr_issue_count)
    saved_metric.metric("已确认证据", len(existing_units))
    target_metric.metric("结构目标", quality["evidence_target"])

    if not pages:
        st.info("尚无可处理页面。请先在“上传”中添加并解析 PDF。")
    else:
        overview_tab, quick_tab, page_tab, library_tab = st.tabs([
            "异常概览", "快速整理", "逐页核对", "证据库",
        ])

        with overview_tab:
            if frozen_evidence:
                st.success("证据分区已冻结。下列疑难片段已被模型跳过；证据数量充足时无需逐条处理。")
            elif evidence_human_queue:
                st.warning("这些页面的原文完整性无法自动确认。优先处理 OCR、少文本页和影响结构目标的缺口。")
            else:
                st.success("当前没有需要人工介入的原文异常。")

            if evidence_human_queue:
                reason_options = sorted({item.get("reason", "未说明原因") for item in evidence_human_queue})
                filter_left, filter_right = st.columns([2, 1])
                with filter_left:
                    reason_filter = st.selectbox(
                        "按异常类型筛选",
                        ["全部异常"] + reason_options,
                        key=f"evidence-reason-filter-{job_id}",
                    )
                with filter_right:
                    show_location = st.toggle(
                        "显示技术定位 ID",
                        value=False,
                        key=f"evidence-show-location-{job_id}",
                    )
                visible_issues = [
                    item for item in evidence_human_queue
                    if reason_filter == "全部异常" or item.get("reason") == reason_filter
                ]
                issue_rows = [
                    {
                        "PDF 页码": item.get("pdf_page"),
                        "异常原因": item.get("reason", "未说明原因"),
                        **({"原文定位 ID": item.get("block_id", "")} if show_location else {}),
                    }
                    for item in visible_issues
                ]
                st.dataframe(
                    issue_rows,
                    hide_index=True,
                    width="stretch",
                    height=min(430, 42 + 35 * len(issue_rows)),
                )
                st.caption(f"当前显示 {len(issue_rows)} / {len(evidence_human_queue)} 项。选择“逐页核对”可直接对照 PDF 原页。")
            else:
                st.markdown(
                    '<div class="aviation-card"><h3>无需处理异常</h3>'
                    '<p>可以继续在“快速整理”中准备证据，或到“证据库”检查已保存结果。</p></div>',
                    unsafe_allow_html=True,
                )

        with quick_tab:
            if frozen_evidence:
                st.info("证据分区已经冻结，批量建议不再接受修改。")
                short_quotes = existing_units and sum(len(item["quote"]) for item in existing_units) / len(existing_units) < 120
                if short_quotes and any(len(page["blocks"]) > 1 for page in pages):
                    st.warning("当前证据平均不足 120 字，部分可能只覆盖页面一行。可以复制任务重建完整证据，原任务不会被修改。")
                    if st.button("复制当前任务并重建完整证据"):
                        new_job_id = action(core.clone_parsed_job, job_id)
                        if new_job_id:
                            st.session_state.job_id = new_job_id
                            st.rerun()
            else:
                st.markdown("#### 批量准备证据")
                st.caption("系统按文档给出连续文本建议。先在表格中编辑主题、章节组与分区，再一次保存。")
                quick_suggestions = []
                for doc in docs:
                    for item in core.evidence_suggestions(job_id, doc["doc_id"]):
                        quick_suggestions.append({"文档": doc["filename"], **item})
                if quick_suggestions:
                    evidence_count = len(existing_units)
                    quick_select_all = st.checkbox(
                        "选中全部建议",
                        key=f"quick-evidence-all-{job_id}-{evidence_count}",
                    )
                    quick_rows = [{**item, "selected": quick_select_all} for item in quick_suggestions]
                    quick_edited = st.data_editor(
                        quick_rows,
                        disabled=["文档", "pdf_page", "block_id", "start_block_id", "end_block_id", "blocks", "ocr_used"],
                        hide_index=True,
                        width="stretch",
                        num_rows="fixed",
                        key=f"quick-evidence-editor-{job_id}-{evidence_count}-{int(quick_select_all)}",
                    )
                    quick_items = quick_edited.to_dict("records") if hasattr(quick_edited, "to_dict") else quick_edited
                    quick_selected = [row for row in quick_items if row["selected"]]
                    action_left, action_right = st.columns([1, 1])
                    with action_left:
                        quick_reviewer = st.text_input("证据核对人 ID", key=f"quick-evidence-reviewer-{job_id}")
                    with action_right:
                        quick_confirm = st.checkbox(
                            "已对照 PDF 抽查，特别是 OCR 与表格",
                            key=f"quick-evidence-confirm-{job_id}-{evidence_count}",
                        )
                    st.caption(f"本次选中 {len(quick_selected)} 条；保存前仍可直接修改表格内容。")
                    if st.button(
                        "保存选中的证据",
                        type="primary",
                        disabled=not (quick_selected and quick_reviewer.strip() and quick_confirm),
                    ):
                        saved = action(core.add_evidence_batch, job_id, quick_selected, quick_reviewer)
                        if saved:
                            st.rerun()
                else:
                    st.success("没有新的批量建议。可进入“逐页核对”补充证据，或在“证据库”检查结果。")

        with page_tab:
            st.markdown("#### 定位原页")
            nav_doc, nav_page = st.columns([2, 1])
            with nav_doc:
                doc_choice = st.selectbox(
                    "文档",
                    [d["doc_id"] for d in docs],
                    format_func=lambda x: next(d["filename"] for d in docs if d["doc_id"] == x),
                    key=f"page-review-doc-{job_id}",
                )
            doc_pages = [p for p in pages if p["doc_id"] == doc_choice]
            page_numbers = [p["pdf_page"] for p in doc_pages]
            flagged_pages = [item["pdf_page"] for item in evidence_human_queue if item.get("pdf_page") in page_numbers]
            default_page = flagged_pages[0] if flagged_pages else page_numbers[0]
            with nav_page:
                page_no = st.selectbox(
                    "PDF 物理页码",
                    page_numbers,
                    index=page_numbers.index(default_page),
                    key=f"page-review-number-{job_id}-{doc_choice}",
                )
            page = next(p for p in doc_pages if p["pdf_page"] == page_no)
            if page["possible_scanned"]:
                st.warning("此页识别后仍只有少量文字，请对照原页检查内容是否完整。")
            elif page.get("ocr_used"):
                st.info("此页文字来自 OCR；引用前请对照原 PDF 核对识别结果。")

            blocks = page["blocks"]
            block = None
            block_id = None
            block_by_id = {b["block_id"]: b for b in blocks}
            block_labels = {
                b["block_id"]: f"{b['block_id'].rsplit('-', 1)[-1]} · {' '.join(b['text'].split())[:72]}"
                for b in blocks
            }
            if blocks:
                flagged_block_id = next(
                    (item.get("block_id") for item in evidence_human_queue if item.get("block_id") in block_by_id),
                    None,
                )
                default_index = (
                    list(block_by_id).index(flagged_block_id)
                    if flagged_block_id
                    else max(range(len(blocks)), key=lambda i: len(blocks[i]["text"].strip()))
                )
                block_id = st.selectbox(
                    "当前文本块",
                    list(block_by_id),
                    index=default_index,
                    format_func=block_labels.get,
                    key=f"page-review-block-{job_id}-{doc_choice}-{page_no}",
                )
                block = block_by_id[block_id]

            preview_left, preview_right = st.columns([1.05, .95], gap="large")
            with preview_left:
                with fitz.open(core.job_dir(job_id) / "raw_pdf" / page["filename"]) as pdf:
                    scale = 1.3
                    pix = pdf[page_no - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
                    picture = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                    if block:
                        draw = ImageDraw.Draw(picture)
                        draw.rectangle([x * scale for x in block["bbox"]], outline="#e6513e", width=4)
                    st.image(picture, caption=f"PDF 第 {page_no} 页 · 红框为当前文本块", width="stretch")
            with preview_right:
                if not block:
                    st.info("本页未解析到文本块。")
                else:
                    st.text_area(
                        "本页识别全文",
                        "\n\n".join(b["text"].strip() for b in blocks),
                        height=360,
                        disabled=True,
                        key=f"page-text-{doc_choice}-{page_no}",
                    )
                    st.text_area(
                        "当前文本块",
                        block["text"],
                        height=165,
                        disabled=True,
                        key=f"selected-text-{doc_choice}-{page_no}-{block_id}",
                    )
                    st.caption(f"本页共 {len(blocks)} 个文本块。表格与跨行条款请使用下方“合并连续块”。")

            single_tab, merge_tab, document_batch_tab = st.tabs([
                "保存当前块", "合并连续块", "当前文档批量建议",
            ])

            with single_tab:
                if block:
                    with st.form("evidence-form"):
                        field_left, field_right = st.columns(2)
                        with field_left:
                            topic = st.text_input("主题")
                            clause = st.text_input("章节 / 相邻条款组 ID")
                        with field_right:
                            split = st.selectbox("数据分区", ["train", "validation", "test"])
                        quote = st.text_area(
                            "精确引用（必须是当前块原文子串）",
                            value=block["text"].strip(),
                            height=130,
                            key=f"quote-{block_id}",
                        )
                        submitted = st.form_submit_button("确认并保存当前证据", type="primary")
                    if submitted:
                        action(core.add_evidence, job_id, block_id, topic, clause, split, quote)
                else:
                    st.info("当前页面没有可保存的文本块。")

            with merge_tab:
                if len(blocks) < 2 or frozen_evidence:
                    st.info("需要至少两个文本块且证据分区尚未冻结，才能合并连续块。")
                else:
                    st.caption("适合表格、多行条款或被 PDF 排版拆开的连续正文。")
                    whole_page = st.checkbox("包含本页全部已解析文字", key=f"whole-page-{doc_choice}-{page_no}")
                    if whole_page:
                        range_start_id, range_end_id = blocks[0]["block_id"], blocks[-1]["block_id"]
                    else:
                        selected_index = next(i for i, b in enumerate(blocks) if b["block_id"] == block_id)
                        range_left, range_right = st.columns(2)
                        with range_left:
                            range_start_id = st.selectbox(
                                "起始文本块",
                                [b["block_id"] for b in blocks],
                                index=min(selected_index, len(blocks) - 2),
                                format_func=block_labels.get,
                                key=f"range-start-{doc_choice}-{page_no}",
                            )
                        with range_right:
                            range_end_id = st.selectbox(
                                "结束文本块（包含）",
                                [b["block_id"] for b in blocks],
                                index=len(blocks) - 1,
                                format_func=block_labels.get,
                                key=f"range-end-{doc_choice}-{page_no}",
                            )
                    try:
                        range_source = core.page_block_range(page, range_start_id, range_end_id)
                    except ValueError as exc:
                        st.warning(str(exc))
                        range_source = None
                    if range_source:
                        with st.form("range-evidence-form"):
                            range_quote = st.text_area(
                                f"合并预览（{len(range_source['block_ids'])} 块）",
                                value=range_source["source_text"],
                                height=240,
                                key=f"range-quote-{range_source['block_id']}",
                            )
                            merge_left, merge_right = st.columns(2)
                            with merge_left:
                                range_topic = st.text_input("合并证据主题")
                                range_group = st.text_input("章节 / 相邻条款组 ID")
                            with merge_right:
                                range_split = st.selectbox("数据分区", ["train", "validation", "test"], key="range-split")
                                range_reviewer = st.text_input("核对人 ID", key="range-reviewer")
                            range_submitted = st.form_submit_button("保存合并证据", type="primary")
                        if range_submitted:
                            result = action(
                                core.add_evidence_range, job_id, doc_choice, page_no,
                                range_start_id, range_end_id, range_topic, range_group,
                                range_split, range_reviewer, range_quote,
                            )
                            if result:
                                st.rerun()

            with document_batch_tab:
                suggestions = core.evidence_suggestions(job_id, doc_choice)
                if suggestions and not frozen_evidence:
                    st.caption("仅显示当前文档建议；适合在逐页核对后补充遗漏片段。")
                    current_count = len(core.evidence_for(job_id))
                    select_all = st.checkbox("选中当前文档全部建议", key=f"batch-all-{job_id}-{doc_choice}-{current_count}")
                    rows = [{**item, "selected": select_all} for item in suggestions]
                    edited = st.data_editor(
                        rows,
                        disabled=["pdf_page", "block_id", "start_block_id", "end_block_id", "blocks", "ocr_used"],
                        hide_index=True,
                        width="stretch",
                        num_rows="fixed",
                        key=f"batch-editor-{job_id}-{doc_choice}-{current_count}-{int(select_all)}",
                    )
                    edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
                    chosen = [row for row in edited_rows if row["selected"]]
                    batch_left, batch_right = st.columns(2)
                    with batch_left:
                        batch_reviewer = st.text_input("批量核对人 ID", key=f"batch-reviewer-{job_id}")
                    with batch_right:
                        batch_confirmed = st.checkbox(
                            "已对照 PDF 核对所选行",
                            key=f"batch-confirm-{job_id}-{doc_choice}-{current_count}",
                        )
                    if st.button(
                        "保存当前文档所选证据",
                        disabled=not (chosen and batch_reviewer.strip() and batch_confirmed),
                    ):
                        result = action(core.add_evidence_batch, job_id, chosen, batch_reviewer)
                        if result:
                            st.success(f"已批量保存 {len(result)} 条证据")
                            st.rerun()
                elif frozen_evidence:
                    st.info("证据分区已经冻结。")
                else:
                    st.info("当前文档没有新的批量建议。")

        with library_tab:
            st.markdown("#### 已确认的证据")
            st.caption("在这里做最终覆盖检查并冻结分区。冻结后不能再增补或修改证据。")
            if existing_units:
                st.dataframe(
                    [
                        {
                            "证据 ID": item["evidence_unit_id"],
                            "文档": item["filename"],
                            "页码": item["pdf_page"],
                            "主题": item["topic"],
                            "章节组": item["clause_group"],
                            "分区": item["split"],
                        }
                        for item in existing_units
                    ],
                    hide_index=True,
                    width="stretch",
                )
                frozen = core.read_json(frozen_path)
                if frozen:
                    st.success(f"分区已冻结：{frozen['counts']}")
                elif st.button(
                    "完成检查并冻结证据分区",
                    type="primary",
                    help="冻结后不能增补或修改证据；请先检查章节组和数据分区。",
                ):
                    result = action(core.freeze_splits, job_id)
                    if result:
                        st.json(result)
            else:
                st.info("尚未保存证据。请先使用“快速整理”或“逐页核对”。")

with t_candidates:
    st.subheader("问答异常与人工处理")
    units = core.evidence_for(job_id)
    if not (core.job_dir(job_id) / "evidence/splits.json").exists():
        st.info("先在上一页冻结证据分区；随后启动本机推理服务，即可生成待审核候选。")
    else:
        st.info("先批量生成 train/validation 候选并检查，再冻结生成配方，即可用同一配方批量生成 test 候选。")
        st.subheader("快速批量处理")
        per_unit = st.number_input("每条证据最多生成多少个不同问法", min_value=1, max_value=30, value=8)
        if st.button("批量生成待审问答", help="可重复运行；已保存的问法会自动跳过，失败项单独记入任务日志。"):
            progress_bar = st.progress(0.0)
            progress_text = st.empty()

            def update_generation_progress(summary):
                progress_bar.progress(summary["processed"] / max(summary["total_planned"], 1))
                progress_text.caption(
                    f"已处理 {summary['processed']}/{summary['total_planned']}；"
                    f"新增 {summary['created']}，已存在 {summary['existing']}，失败 {summary['failed']}"
                )

            with st.spinner("本机模型正在批量生成，可能需要数分钟"):
                result = action(generation.generate_batch, job_id, int(per_unit), 300, update_generation_progress)
            if result:
                st.json(result)
                st.rerun()

        recipe_path = core.job_dir(job_id) / "candidates/generation_recipe.json"
        if not recipe_path.exists():
            if st.button("检查开发集候选后冻结生成配方"):
                result = action(generation.freeze_recipe, job_id)
                if result:
                    st.rerun()
        else:
            st.success("生成配方已冻结；可批量生成 test 候选。")
            if st.button("批量生成 test 待审问答"):
                test_progress = st.progress(0.0)
                def update_test_progress(summary):
                    test_progress.progress(summary["processed"] / max(summary["total_planned"], 1))
                with st.spinner("正在用冻结配方生成测试集候选"):
                    result = action(generation.generate_batch, job_id, int(per_unit), 300, update_test_progress, "test")
                if result:
                    st.json(result)
                    st.rerun()

        expert_count = sum(r["review"]["status"] == "pending_expert_review" for r in core.candidates_for(job_id))
        if expert_count:
            st.warning(f"有 {expert_count} 条模型无法可靠判断的问答，请优先核对原 PDF 与证据。")
        queue = workflow.review_queue(core.candidates_for(job_id), units)
        pending_count = len(queue)
        valid_queue = [item for item in queue if not item["errors"]]
        invalid_queue = [item for item in queue if item["errors"]]
        if pending_count:
            st.write(f"待审核 {pending_count} 条；结构有效 {len(valid_queue)} 条；需修正 {len(invalid_queue)} 条。带 OCR 标记的问答排在前面。")
            if invalid_queue:
                st.warning("结构异常样本需在下方逐条修正：" + "、".join(item["record"]["sample_id"] for item in invalid_queue[:10]))
        if valid_queue:
            review_page_size = st.selectbox("每页审核数量", [25, 50, 100], index=1)
            total_pages = (len(valid_queue) + review_page_size - 1) // review_page_size
            review_page = st.number_input("审核页码", min_value=1, max_value=total_pages, value=1,
                                          key=f"review-page-{job_id}-{len(valid_queue)}-{review_page_size}")
            visible_queue = valid_queue[(review_page - 1) * review_page_size:review_page * review_page_size]
            select_all_reviews = st.checkbox(
                "选中本页全部已核对候选",
                key=f"review-all-{job_id}-{pending_count}-{review_page}",
            )
            review_rows = [
                {
                    "selected": select_all_reviews,
                    "sample_id": item["record"]["sample_id"],
                    "split": item["record"]["split"],
                    "需核对": "、".join(item["flags"]),
                    "question": item["record"]["question"],
                    "answer": item["record"]["answer"],
                    "evidence": " | ".join(ref["quote"][:200] for ref in item["record"]["evidence"]),
                }
                for item in visible_queue
            ]
            review_table = st.data_editor(
                review_rows,
                disabled=["sample_id", "split", "需核对", "question", "answer", "evidence"],
                hide_index=True, width="stretch", num_rows="fixed",
                key=f"review-editor-{job_id}-{pending_count}-{review_page}-{int(select_all_reviews)}",
            )
            review_items = review_table.to_dict("records") if hasattr(review_table, "to_dict") else review_table
            selected_review_ids = [row["sample_id"] for row in review_items if row["selected"]]
            st.caption(f"本页 {review_page}/{total_pages}；已选 {len(selected_review_ids)} 条。结构校验只检查引用和字段，答案语义仍需核对。")
            batch_decision = st.selectbox(
                "批量审核结论", ["approved", "pending_expert_review", "rejected"],
                key=f"review-decision-{job_id}",
            )
            batch_reviewer_id = st.text_input("批量审核人 ID", key=f"reviewer-{job_id}")
            batch_comment = st.text_input("批量审核备注", key=f"review-comment-{job_id}")
            reviewed_selected = st.checkbox(
                "我已检查本页所选问答及其证据，确认使用此审核结论",
                key=f"review-confirm-{job_id}-{pending_count}-{review_page}",
            )
            if st.button(
                "提交批量审核",
                disabled=not (selected_review_ids and batch_reviewer_id.strip() and reviewed_selected),
            ):
                result = action(
                    core.review_candidates_batch, job_id, selected_review_ids,
                    batch_reviewer_id, batch_decision, batch_comment,
                )
                if result:
                    st.json(result)
                    st.rerun()

        with st.expander("高级：逐条生成、录入与修正", expanded=False):
            st.divider()
            st.subheader("逐条处理")
            if st.button("检查本机模型服务"):
                result = action(generation.probe)
                if result:
                    st.json(result)
            available = [e for e in units if e["split"] in {"train", "validation"}]
            if available:
                gen_id = st.selectbox("自动生成所用证据", [e["evidence_unit_id"] for e in available], format_func=lambda x: next(f"{e['topic']} · {e['split']} · {x}" for e in available if e["evidence_unit_id"] == x))
                variant = st.selectbox("生成变体", list(generation.VARIANTS), format_func=lambda x: {"fact": "单证据事实", "condition": "条件/例外", "unanswerable": "证据不足/拒答"}[x])
                if st.button("生成 1 条待审核候选"):
                    result = action(generation.generate_candidate, job_id, gen_id, variant)
                    if result:
                        st.json(result)
                        st.rerun()
            st.divider()
            st.write("或手工录入候选：")
            with st.form("candidate-form"):
                chosen = st.multiselect("引用证据单元", [e["evidence_unit_id"] for e in units], format_func=lambda x: next(f"{e['topic']} · {e['split']} · {x}" for e in units if e["evidence_unit_id"] == x))
                question = st.text_area("问题")
                answerability = st.selectbox("可回答状态", ["answerable", "unanswerable"])
                claim = st.text_area("原子化关键结论；不可回答时留空")
                answer = st.text_area("答案；可回答时使用 [E1] 等引用，拒答时明确证据不足")
                submitted = st.form_submit_button("保存为未审核候选")
            if submitted:
                action(core.add_candidate, job_id, question, answer, chosen, answerability, claim)
            records = core.candidates_for(job_id)
            if records:
                statuses = {r["review"]["status"] for r in records}
                status_filter = st.selectbox("按审核状态筛选", ["全部"] + sorted(statuses))
                visible = [r for r in records if status_filter == "全部" or r["review"]["status"] == status_filter]
                if visible:
                    sample_id = st.selectbox("候选样本", [r["sample_id"] for r in visible])
                    record = next(r for r in visible if r["sample_id"] == sample_id)
                    st.json(record)
                    errors = core.validate_candidate(record, {e["evidence_unit_id"]: e for e in units})
                    if errors:
                        st.error("自动校验问题：" + "；".join(errors))
                    else:
                        st.success("自动结构与原文定位校验通过；语义是否成立仍须人工判断。")
                    with st.expander("修正候选 JSON（保留审核历史）"):
                        editor = st.text_input("编辑人 ID")
                        revised_text = st.text_area("候选 JSON", value=json.dumps(record, ensure_ascii=False, indent=2), height=320)
                        if st.button("保存修正并重置为待审核"):
                            try:
                                revised = json.loads(revised_text)
                                action(core.edit_candidate, job_id, sample_id, revised, editor)
                            except Exception as exc:
                                st.error(str(exc))
                    with st.form("review-form"):
                        reviewer = st.text_input("审核人 ID")
                        decision = st.selectbox("审核决定", ["approved", "regenerate", "rejected", "pending_expert_review"])
                        comment = st.text_area("审核说明 / 问题")
                        reviewed = st.form_submit_button("提交审核记录")
                    if reviewed:
                        action(core.review_candidate, job_id, sample_id, decision, reviewer, comment)

with t_export:
    st.subheader("质量门槛与本地下载")
    report = core.quality_report(job_id)
    state_title = "结构门槛已达标" if report["final_ready"] else "结构门槛待完成"
    state_detail = "可导出审计包；语义质量仍需独立抽样验收。" if report["final_ready"] else f"还有 {len(report['issues'])} 项需要解决，预览包不能用于训练。"
    st.markdown(f'<div class="aviation-card"><h3>{state_title}</h3><p>{state_detail}</p></div>', unsafe_allow_html=True)
    st.caption("当前模型同时负责生成与双重核验，0.90 是模型自报值，不能当作真实准确率。")
    if report["approved_by_document"]:
        st.dataframe([{"文档": name, "获批问答": count, "被引用证据": report["approved_evidence_units_by_document"][name]} for name, count in report["approved_by_document"].items()], hide_index=True, width="stretch")
    if report["issues"]:
        st.warning("；".join(report["issues"][:8]))
    with st.expander("查看完整质量报告"):
        st.json(report)
    left, right = st.columns(2)
    with left:
        if st.button("生成预览包（不可训练）"):
            action(core.export_job, job_id, False)
    with right:
        if st.button("导出结构达标包（语义待验收）", disabled=not report["final_ready"]):
            action(core.export_job, job_id, True)
    for kind in ("final", "preview"):
        path = core.job_dir(job_id) / "exports" / f"aviation-sft-{kind}.zip"
        if path.exists():
            digest = core.sha256_file(path)
            sidecar = core.read_json(path.with_suffix(".sha256.json"), {})
            candidate_path = core.job_dir(job_id) / "candidates/records.jsonl"
            evidence_path = core.job_dir(job_id) / "evidence/units.jsonl"
            stale = sidecar.get("candidates_sha256") != (core.sha256_file(candidate_path) if candidate_path.exists() else None) or sidecar.get("evidence_sha256") != (core.sha256_file(evidence_path) if evidence_path.exists() else None)
            st.write(f"{path.name} · SHA-256 `{digest}`")
            stale = stale or core.sources.artifact_status(job_id, path, sidecar)["stale"]
            if stale:
                st.warning("此包生成后候选或证据发生变化，请重新导出。")
            else:
                with path.open("rb") as f:
                    st.download_button(f"下载 {kind} ZIP 到本地", data=f.read(), file_name=path.name, mime="application/zip", key=f"dl-{kind}")
    st.divider()
    st.subheader("确认本地保存后清理服务器任务")
    st.warning("删除整个任务目录，包括上传 PDF、解析数据、审核历史与导出包；不会删除模型，也不会删除你电脑上的文件。请先校验本地 ZIP SHA-256。")
    confirm_id = st.text_input("输入完整任务 ID 确认")
    confirm_sha = st.text_input("输入已下载 ZIP 的 SHA-256")
    if st.button("永久删除本任务服务器文件", type="secondary"):
        if action(core.cleanup_job, job_id, confirm_id, confirm_sha) is None:
            if not (core.jobs_root() / job_id).exists():
                st.session_state.pop("job_id", None)
                st.rerun()
