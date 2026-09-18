# 民航回答微调数据集构建器

上传 PDF，整理可追溯文本与表格，选取证据、生成问答、执行模型审核，并下载数据集。

本项目是数据整理与微调数据集构建工具，不负责实际模型训练。当前为实验版本：自动审核通过不等于内容正确。

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

网页自动构建按钮使用 `screen` 启动后台任务，Linux 用户需先安装 `screen`。不使用后台按钮时，可在已激活环境的终端直接执行：

```bash
aviation-ft autopilot JOB_ID --until-ready
aviation-ft status JOB_ID
```

## 模型配置

- 外部 API：在网页创建任务后，进入“模型 API”，配置兼容接口、模型名和 API Key，并确认文本外发范围。
- 本地模型：默认连接 http://127.0.0.1:8000/v1 ，服务名称为 `aviation-generator`。在独立环境安装并启动兼容推理服务，权重放在 `models/generator/`；模型相关参数位于 `configs/generation.json`。
- 默认本地模型检查要求 `model.safetensors.index.json`；其他权重格式需要调整校验逻辑。
- 密钥保存在本机 `runtime/provider_secrets/`，不随源码分发。外部 API 费用由使用者承担。

## OCR

带文字层的 PDF 可直接解析。扫描 PDF 需要 Tesseract 语言数据，将 `chi_sim.traineddata`、`eng.traineddata` 放到 `models/ocr/tessdata/`。语言和 DPI 在 `configs/settings.json` 配置。源码包不附带语言数据或模型权重。

## 使用流程

1. 创建任务并上传有权处理的 PDF。
2. 解析并下载整理后的 TXT，核对条款和表格。
3. 配置模型，启动证据筛选、问答生成和审核。
4. 在“结果交付”下载精简训练包，必要时下载完整追溯包。

训练包仅含 `train.jsonl`、`validation.jsonl`、`test.jsonl`、`README.txt`。完整追溯包额外包含证据、来源、生成配置和审核日志。TXT 单独下载。所有运行数据保存在 `runtime/`，源码下载不包含已有任务。

## 当前限制

- 每份 PDF 默认选取 20 条证据，并以 90 条审核通过问答作为总量门槛；目前不是全文章节覆盖式生成。
- 多种问法可能产生语义重复，已有测试发现错误拒答可通过同模型自动审核，数据用于训练前需要内容验收。
- TXT 的文字清理、表格和章节整理属于独立输出，目前不会改写已经冻结的训练证据。
- 生成与审核使用同一模型，不构成独立语义验证。
- 复杂版式、扫描表格和公式仍需对照 PDF 核对。
- 当前推荐从源码进行 editable 安装，配置和提示词使用项目根目录；不宣称支持脱离源码目录的独立 wheel 部署。

## 开发检查

```bash
python -m pytest tests -q
```

## 发布范围

此包只包含源码、测试、提示词和无密钥默认配置。不包含用户 PDF、生成数据集、运行日志、模型权重、OCR 语言包和备份。

项目授权许可尚待仓库所有者选择；本准备包没有代替所有者授予开源许可。第三方依赖和模型各自的许可仍适用。
