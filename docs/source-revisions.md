# 来源修订接口与持久化约定

本功能用于整页来源选择和单原始块文字校正，不支持任意局部来源混合。页面、API、自动化、Streamlit、TXT 和导出共用业务层的有效来源及过期检查。项目没有账户认证，`reviewer` 是操作人声明，不是认证身份。

## HTTP 接口

统一前缀：`/api/jobs/{job_id}`。JSON 响应禁止缓存。新接口错误返回 `{"error":"说明"}`，400 为参数无效，404 为资源不存在，409 为内容版本、哈希或操作 ID 冲突，403 为冻结禁止修改。原有接口保留原错误约定。

| 方法与路径 | 输入 | 返回 |
|---|---|---|
| `GET /page-source` | 查询参数 `doc_id`、`page`（从 1 开始） | `current`、`candidates`、`comparison`、`risks`、`source_verified`、`requires_verification`、`frozen`、`legacy_token`、页面尺寸及历史候选缺失提示 |
| `POST /page-source/preview` | `operation_type`、`doc_id`、`pdf_page`、`expected_revision`；选择操作带 `candidate_id`，校正操作带 `candidate_id`、`block_id`、`corrected_text` | 当前基线版本、拟采用来源和完整块、比较结果、`impact`；不保存 |
| `POST /page-source/select` | 公共提交参数及 `candidate_id` | 统一提交结果 |
| `POST /page-source/correct` | 公共提交参数及 `candidate_id`、`block_id`、`corrected_text` | 统一提交结果 |
| `POST /page-source/initialize` | 公共提交参数，`expected_revision` 使用读取时返回的 `legacy_token` | 建立历史来源基线，候选 ID 为 `null`，状态待核实 |
| `POST /page-source/verify` | 公共提交参数，绑定具体当前版本 | 记录人工核实，不创建内容版本，不使旧证据恢复有效 |
| `POST /clone` | 可选 `label` | 201，`{"job_id":"新任务ID"}` |

公共提交参数：`doc_id`、`pdf_page`、`expected_revision`、`operation_id`、`reviewer`、`reason`。操作 ID 为 1–100 个字母、数字、下划线或连字符；同一次提交重试必须使用同一 ID。预览操作类型为 `select_source`、`correct_text`、`initialize`、`verify`。校正历史来源基线时，`candidate_id` 为 `null`。

统一提交结果：`changed`（内容版本是否改变）、`current_revision`、`operation_id`、`summary`、`impact`、`next_step`。核实操作的 `changed=false` 表示未创建内容版本，不表示核实记录没有保存。相同内容及来源不创建多余版本；同操作 ID、同请求重试返回原提交结果，同 ID 不同请求返回 409。

服务端只从保存的候选和版本读取旧文字、块序、页码身份与坐标。前端附带的旧文字或坐标不作为候选内容。路径中的任务、请求中的文档页和候选必须一致。PDF 和候选哈希不匹配时拒绝提交。

`comparison` 包含规范化文字、规范化字符到原始文本偏移的数组、`difference_rate` 和 `changes`。变化类型为 `insert`、`delete`、`replace`；左右片段各含原始文字、原始及规范化范围、块定位与精度，`highlights` 标识数字、单位和否定词。只规范化空白，不改写原始内容。块框和页级定位不代表精确 PDF 字符框。

`impact` 包含 `semantic_ids`、`evidence_ids`、`sample_ids`、数量、是否存在 TXT 及导出文件名。影响策略为页级保守失效，包含跨页单元；不会声称逐字符精确影响范围。

## 存储与提交

存储位于 `runtime/jobs/{job_id}/parsed/sources/`：

- `candidates/{id}.json`：不可变原生/OCR 候选，含任务、文档页、PDF 哈希、原始块和文本哈希。状态在清单中区分 `available`、`not_generated`、`failed`；可用候选的块列表为空表示成功但空文本。
- `revisions/{id}.json`：不可变完整版本，含 `schema_version=1`、`source_revision_id`、`parent_revision_id`、`source_candidate_id`、`source_text_sha256`、`pdf_sha256`、`source_kind`、`blocks`、定位精度及操作记录。来源类型为 `native`、`ocr`、`manual`、`legacy`。
- `manifest.json`：页到当前版本的指针、版本文件哈希、候选索引、核实要求及已核实版本、已提交操作索引。复制任务保留继承的操作历史，重置新任务的幂等索引。

格式版本不是内容版本。内容版本使用唯一 ID；原始 `parsed/pages.jsonl` 保持不变，`core.pages_for()` 叠加已提交的有效版本。候选不可修改；修改只生成新版本。

操作记录包含 `operation_id`、`operation_type`、文档页、基线和新版本、候选及块 ID、前后文字哈希、操作人、依据与时间。校正另外保存服务器确认的前后块文字及原始文字映射；新增文字没有独立字符框时保持块级精度。核实与初始化是明确的附加操作类型。

任务锁使用 Windows 字节锁或 POSIX 文件锁，并支持同线程重入。来源提交与冻结、证据、审核和成果写入互斥；模型请求不在锁内执行，模型结果写入时重新校验来源。版本文件先写入并同步，再原子替换清单；写入失败留下的未提交文件不会被当前读取或操作索引采用。

## 下游与兼容

证据新增 `source_revisions`，绑定所有涉及页的版本或旧页内容令牌，及 `source_location_precision=block`。读取时的 `source_issues` 是派生状态，不写入历史证据文件。问答引用复制版本绑定，保持精确引用校验，不允许借校正放宽匹配；引用重复子串而无法唯一定位时拒绝。

过期证据、依赖问答及待核实风险不计入当前有效数量，不能满足冻结、审核、生成和正式交付门槛。来源风险页包括切换、校正、初始化、OCR 使用或失败以及少文本页；其他页不引入全量人工核实门槛。核实绑定具体版本，不能清除原有结构问题或复活旧证据。

TXT 报告和导出侧车文件新增 `source_snapshot`。成果读取检查来源快照，ZIP 同时检查原有证据及问答文件哈希。没有来源快照的历史成果在任务建立来源版本后按过期处理；未初始化旧任务不自动迁移。重建成果保存旧文件及其已有元信息，完整追溯 ZIP 包含来源历史。训练包维持原有四文件格式。

冻结任务的来源编辑与初始化返回 403。复制任务保留 PDF、解析、来源版本和历史，清空证据及问答；候选在新任务中登记身份和哈希。原冻结任务的证据、审核和文件哈希保持不变。
