# 民航回答微调数据集构建器

上传 PDF，整理可追溯文本与表格，选取证据、生成问答、执行模型审核，并下载数据集。

本项目是数据整理与微调数据集构建工具，不负责实际模型训练。当前为实验版本；自动通过默认关闭。规则检查、对照 PDF 的来源核实和问答事实审核是三个不同状态。

## 安装与启动

推荐 Linux，Python 3.11 或更高版本。网页无需 GPU；调用外部模型 API 需要用户自行配置接口与密钥。本地模型服务单独安装、下载权重并配置计算资源。

下载并解压源码，进入项目根目录：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
bash scripts/run-web.sh
```

打开 http://127.0.0.1:8501 。界面没有账户认证，默认仅供本机访问。服务器部署可通过 SSH 隧道访问。

Windows 已安装 editable 环境时，可执行 `\.venv\Scripts\python.exe -m aviation_ft.web_app.server`。本地 Streamlit 审核界面可执行 `streamlit run src/aviation_ft/review_app/app.py`。

网页自动构建按钮使用 `screen` 启动后台任务，Linux 用户需先安装 `screen`。不使用后台按钮时，可在已激活环境的终端直接执行：

```bash
aviation-ft autopilot JOB_ID --until-ready
aviation-ft status JOB_ID
```

## 模型配置

- 外部 API：在网页创建任务后，进入“模型 API”，分别选择生成、主评审和反例评审角色，配置兼容接口、模型名和 API Key，并确认文本外发范围。每个角色单独保存配置；只配置生成角色时不会得到独立评审。
- 本地模型：默认连接 http://127.0.0.1:8000/v1 ，服务名称为 `aviation-generator`。在独立环境安装并启动兼容推理服务，权重放在 `models/generator/`；模型相关参数位于 `configs/generation.json`。
- 本地独立评审：在 `configs/review.json` 的 `roles.judge_primary` 和 `roles.judge_countercheck` 中分别设置 `model_id`、`model_revision`、`model_dir`、`base_url` 和 `served_model_name`；网页的角色选择可切换各自的外部 API 或本地模式。缺少独立角色配置时评审不能自动通过。
- 默认本地模型检查要求 `model.safetensors.index.json`；其他权重格式需要调整校验逻辑。
- 密钥保存在本机 `runtime/provider_secrets/`，不写入导出包。Windows 的 POSIX `0600` 位不能证明文件 ACL 隔离，部署时应检查该目录的操作系统访问控制。外部 API 费用由使用者承担。

## OCR

带文字层的 PDF 可直接解析；发现乱码、私用区字符或大面积图片与少量文字等信号时，会尝试 OCR。原生与 OCR 候选均保存在 `parsed/extraction_candidates.jsonl`，冲突和失败保存在 `parsed/issues.jsonl`，逐页指标保存在 `parsed/page_quality.jsonl`。面积比表示版面覆盖，不是文字准确率。扫描 PDF 需要 Tesseract 语言数据，将 `chi_sim.traineddata`、`eng.traineddata` 放到 `models/ocr/tessdata/`。语言和 DPI 在 `configs/settings.json` 配置；初始质量阈值在 `configs/quality.json`，需用真实样本验证。

## 使用流程

1. 创建任务并上传有权处理的 PDF；在“数据中心”解析。查看逐页问题、原生/OCR 候选和整理 TXT。TXT 标出版本、待核实问题及展示排除项，不能替代 PDF。
2. 在“原文证据”处理提取问题，检查逐页文字与 PDF。跨页续接需要在“跨页建议”显式确认；保存的证据先是 draft，再逐条执行“对照 PDF 核实”。所有证据完成来源核实后才能冻结分区。
3. 配置生成模型；问答输出使用 `qa-v2` 多断言结构。生成、覆盖、数值和模型评审依次执行。任何不确定或不支持的事实进入人工复核；主网页或 Streamlit 可逐断言、逐来源核对后提交，过期版本返回冲突。
4. 在“结果交付”查看结构、来源、事实审核三种门槛。预览包仅用于审查；正式包须全部门槛满足。上游解析、审核或配置变化后，旧包会被识别为历史快照，需重新导出。

训练包仅含 `train.jsonl`、`validation.jsonl`、`test.jsonl`、`README.txt`。完整追溯包额外包含解析候选、逐页报告、问题与处理事件、来源片段、生成配置和审核日志。TXT 单独下载。所有运行数据保存在 `runtime/`，源码下载不包含已有任务。字符范围统一是所指不可变文本的 Python Unicode 左闭右开索引；前端使用服务端精确片段，不直接混用 UTF-16 偏移。

## 旧任务迁移与自动通过校准

旧数据可读，但旧证据不会自动标为来源核实，旧单断言候选的覆盖状态为 `not_checked`。迁移默认仅预览；执行时复制原 PDF 和历史审计到新任务并重新解析，原任务不变：

```bash
aviation-ft migrate JOB_ID
aviation-ft migrate JOB_ID --execute
```

`configs/review.json` 初始 `auto_approve_enabled=false` 且 `calibration_id=null`。即使两轮模型均支持，没有有效校准证书和已验证范围也不能自动通过。离线人工标注采用 UTF-8 JSONL，每行字段为 `sample_id`、`doc_id`、`clause_group`、`phase`（`calibration`/`acceptance`）、`gold`（`correct`/`incorrect`）、`decision`（`approved`/`rejected`/`needs_human`）、`reviewer`。校准和独立验收必须按文档与条款组隔离。运行 `python scripts/evaluate_review.py labels.jsonl` 可报告自动通过错误率、错误样本放行率、正确样本误拒率、转人工率和自动覆盖率，各含样本量与 95% Wilson 区间。达到严格验收门槛后才可显式添加 `--job-id JOB_ID --calibration-id ID --write-certificate`；此命令只写本地证书，不会自行启用自动通过。真实标注样本不足时保持关闭。

## 当前限制

- 每份 PDF 默认选取 20 条证据，并以 90 条审核通过问答作为总量门槛；目前不是全文章节覆盖式生成。
- 多种问法可能产生语义重复；规则和模拟测试不能证明真实 PDF 的提取准确率或真实模型的问答可靠性。
- 对复杂表格、公式、文字错序及无法可靠解析的数值关系需要人工核对。当前跨页自动合并仅提出建议；续表列结构未经核实不能自动合并。
- 没有真实人工标注的校准集和独立验收集时，不宣称自动通过错误率达到目标。
- 当前推荐从源码进行 editable 安装，配置和提示词使用项目根目录；不宣称支持脱离源码目录的独立 wheel 部署。

## 开发检查

```bash
python -m pytest tests -q
node --check src/aviation_ft/web_app/static/review_v2.js
```

## 发布范围

此包只包含源码、测试、提示词和无密钥默认配置。不包含用户 PDF、生成数据集、运行日志、模型权重、OCR 语言包和备份。

项目授权许可尚待仓库所有者选择；本准备包没有代替所有者授予开源许可。第三方依赖和模型各自的许可仍适用。
