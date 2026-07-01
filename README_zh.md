# SCCGBench

[English](README.md) | [中文版](README_zh.md)

面向真实服务调用代码生成的数据集。从 RapidAPI 端点文档出发，爬取 GitHub 上真实开发者编写的 API 调用函数，经多层检索、质量打分、文档对齐、模板去污染与意图生成构建，可用于评测代码生成模型。

规模：3,135 条样本 · 850 个 API · 2,082 个 GitHub 仓库。

---

## 简介

现有的 API 代码生成数据集大多依赖**模板生成**或**教程示例**，与真实开发场景存在差距。
**SCCGBench**（**S**ervice-**C**all **C**ode **G**eneration **Bench**mark）的目标是收集**真实、可映射到 API 文档、包含完整请求构建与响应处理逻辑**的函数：

- 函数来自真实 GitHub 仓库，而非自动生成的模板代码
- 每个函数都能对齐到对应的 RapidAPI 端点（endpoint URL / host / 参数文档）
- 覆盖完整调用链路：凭证加载 → 构造请求 → 发起调用 → 状态检查 → 响应解析 → 错误处理
- 剔除全部模板/工具生成的包装代码，并对缺注释样本以**脱敏意图**生成自然语言需求

> **数据安全**：发布代码样本中的凭据型字面值统一替换为分类明确的 `<REDACTED_*>`
> 占位符。发布前审计覆盖 API Key、JWT、Basic/Bearer 凭据、Cookie、密码、Access Token、
> 邮箱和本地用户路径。每次公开发布前请运行
> `python3 03_dataset_construction/audit_public_release.py`。

## 数据集概览

| 指标 | 数值 |
|---|---|
| 函数样本数 | **3,135** |
| 覆盖唯一 API | **850** |
| 覆盖唯一 GitHub 仓库 | **2,082** |
| 数据划分（训练 / 验证 / 测试） | 2,257 / 266 / 612 |
| 真实人工注释 / 脱敏意图注释 | 1,726 / 1,409 |
| 平均质量分（0–100）* | 89.82 |
| 质量分 ≥ 90 / ≥ 80 / ≥ 70 * | 1,675 / 2,618 / 2,708 |
| 注释覆盖率 / docstring 覆盖率 | 65.90% / 25.01% |
| HTTP 方法（GET / POST） | 2,537 / 598 |

**文档对齐类型**：URL 精确匹配 1,935 · route 匹配 462 · host-only 弱对齐 738
（精确对齐 url+route = 2,397，占 76.5%）

### RapidAPI 服务覆盖统计（原论文 Table III）

下表补充论文压缩后从正文中移出的 RapidAPI 覆盖统计信息，用于说明 SCCGBench 覆盖的 API 并非集中在少数服务或低可用性接口上，而是具有一定长尾性、可复现性和真实服务分布特征。

| 指标 | 数值 / 说明 |
|---|---|
| 覆盖唯一 RapidAPI API | **850** |
| 仅出现 1 个样本的 API | **418** |
| Top 20% API 的样本占比 | **64%** |
| 来自订阅数 ≥ 1,000 的服务样本占比 | **约 71%** |
| FREE / FREEMIUM 接口样本占比 | **约 84%** |
| 精确接口对齐样本（url + route） | **2,397 / 3,135（76.5%）** |
| host-only 弱对齐样本 | **738 / 3,135（23.5%）** |

> 注：上述统计基于最终发布的 3,135 条样本及其对齐到的 RapidAPI 元数据。
> 其中订阅数与定价类型用于刻画服务可访问性和复现友好性；API 样本分布用于说明数据集具有明显长尾特征，避免由少数热门 API 主导。

数据分布：

| 代码复杂度（LOC 累积分布） | API 多样性（Lorenz 集中度） |
|---|---|
| ![complexity](figures/fig3.png) | ![diversity](figures/fig4.png) |

![仓库更新频率](figures/fig5.png)

> \* 平均质量分与质量分桶覆盖 2,757 条带 `code_quality_score` 的原始爬取样本；
> 另有 378 条扩量样本（`source_machine=crawl_v2v3`）使用了独立的对齐/质量审计流程。

---

## 数据集构建流程

整个构建分为四个阶段，从 RapidAPI 元数据采集，到 GitHub 真实代码爬取，
再到清洗构建与实验评估：

![构建与评测流程](figures/fig1.png)

### ① RapidAPI 元数据采集 · `01_rapidapi_crawler/`

通过浏览器自动化（CDP / Playwright）从 RapidAPI Hub 抓取 API 列表与端点元数据，
为后续 GitHub 检索提供"线索"（host、endpoint URL、HTTP method、参数文档）。

| 文件 | 作用 |
|---|---|
| `rapidapi_search_cards_cdp.js` | 关键词搜索抓取 API 卡片链接（驱动本机 Edge/Chrome） |
| `rapidapi_crawl_category_cards.js` | 按分类批量抓取卡片（走 GraphQL 网关） |
| `rapidapi_crawl_api_metadata.js` | 抓取单个 API 的端点元数据 |
| `rapidapi_crawl_category_api_metadata.js` | 批量调度，抓取全分类 API 元数据 |

### ② GitHub 真实代码爬取 · `02_github_code_crawler/`

以①得到的端点信息为锚点，在 GitHub 上做**多层检索**，抽取真实的 API 调用函数。
检索从精确到宽松分层（见 `config.py` 的 `QUERY_STRATEGIES`）：

```
endpoint URL 精确匹配 → route + host 匹配 → host 匹配
        → header 匹配 → 库匹配 (requests) → 函数定义匹配
```

核心爬取各检索层贡献样本数（约 2,757 条）：`host_match` 1237 · `endpoint_url_match` 551 ·
`library_match` 246 · `header_match` 234 · `route_host` 223 · `function_match` 156 · 其它 110；
另有 378 条来自后续补充扩量爬取（v2/v3）。

| 文件 | 作用 |
|---|---|
| `github_crawler_v3_enhanced.py` | 核心爬虫：多层检索、质量打分、工具生成代码检测、相似度去重、Token 轮换、并发与断点续跑 |
| `config.py` | 检索策略、质量阈值、注释加分、爬取参数限制 |
| `runtime_config.py` | 运行时输出路径与 token / API 列表加载（单机） |
| `token.json.example` | GitHub Token 模板（复制为 `token.json` 填入自己的 token） |

### ③ 清洗 / 合并 / 构建 · `03_dataset_construction/`

把爬取结果合并、去重、质量筛选，并与 RapidAPI 文档对齐，最后对缺注释样本生成脱敏意图。

| 文件 | 作用 |
|---|---|
| `merge_results.py` | 合并结果 → 哈希去重 → 质量筛选 → 文档对齐 → 生成质量报告 |
| `materialize_machine_results.py` | 把连续写入的 JSONL 物化成 JSON + 统计（中断恢复用） |
| `validate_final_dataset.py` | 最终数据集校验（数量区间、质量、去重一致性） |
| `audit_public_release.py` | 发布前安全审计：凭据、敏感字面值、隐私路径、邮箱、raw 文件与 split 一致性 |
| `generate_supplementary_annotations.py` | 对无注释/无 docstring 样本补充解释性/脱敏意图注释 |

### ④ 实验 / 评估 · `04_experiments/`

| 文件 | 作用 |
|---|---|
| `main_experiment_pipeline.py` | API 级数据划分、C0–C4 上下文提示词、endpoint 选择实验、自动评估、结果汇总 |
| `run_openai_compatible_model.py` | 调用 DeepSeek API 或本地 vLLM/OpenAI 兼容服务（API key 仅从环境变量读） |
| `evaluation_metrics.py` | 评估指标实现 |
| `dense_retrieval_baseline.py` | 稠密检索基线：从发布数据集构建端点语料，用 neural sentence encoder 打分，计算 Host@K / Endpoint@K，与 BM25 共享同一命中判定 |
| `passk_evaluation.py` | pass@$k$ 评估 driver：复用现有生成 + Mock 评估，按无偏估计计算 pass@$k$（回应“解码单一”威胁） |
| `real_call_sanity_check.py` | 真实调用 sanity check：对 FREE RapidAPI 做小规模、安全过滤的真实 2xx 校验，作为 Mock 的正向预测有效性证据（需设 `RAPIDAPI_KEY` 环境变量） |

**自动评估指标（9 项）**：Syntax · Host · Endpoint · Method · Header ·
Parameter F1 · Response Handling · Error Handling · Mock Execution Pass。
（Mock Execution 不真实访问 RapidAPI，仅拦截 `requests` 调用检查请求构造。）

附：`strengthened_real_call_summary.json` 为真实调用 sanity check 的汇总结果，`strengthened_real_call_table.tex` 为可直接引用的 LaTeX 表。

---

## 数据格式

![数据样例](figures/fig2.png)

`dataset/sccgbench_3135.json` 为 JSON 数组，每条记录字段如下：

| 字段 | 说明 |
|---|---|
| `sample_id` | 样本唯一 ID（如 `SCG-000001`） |
| `api_name` / `api_host` | API 名称与 RapidAPI host |
| `function_name` | 函数名 |
| `language` | 编程语言（以 Python 为主） |
| `github_info` | 来源仓库、文件路径、URL、stars、更新时间等 |
| `code` | `complete_function` 等（凭证已脱敏为占位符） |
| `api_metadata` | 对齐到的端点 `url` / `method` / `headers` / `params` / `doc_match_type` |
| `quality_metrics` | 质量分与完整性检查 |
| `comments` / `comments_source` | 注释文本及来源（`original` = 真实人工注释；其它 = 脱敏意图） |
| `source_machine` | 爬取批次标识 |

```jsonc
{
  "sample_id": "SCG-000001",
  "api_name": "kiwi-com-cheap-flights",
  "api_host": "kiwi-com-cheap-flights.p.rapidapi.com",
  "function_name": "search_flights",
  "language": "python",
  "github_info": { "repo": "ScoV8k/travel-app", "file_path": "backend/.../flights.py", "stars": 0 },
  "code": { "complete_function": "import requests\nheaders={'X-RapidAPI-Key':'<REDACTED_RAPIDAPI_KEY>'}\n..." },
  "api_metadata": { "url": "https://kiwi-com-cheap-flights.p.rapidapi.com/round-trip", "method": "GET", "doc_match_type": "url" },
  "quality_metrics": { "code_quality_score": 100.0, "completeness_checks": { "has_api_call": true } },
  "comments_source": "original",
  "source_machine": "machine1"
}
```

数据集文件清单：

```
dataset/
├── sccgbench_3135.json              # 最终数据集（3,135 条，凭证已脱敏）
├── splits/
│   ├── train.json                   # 训练集 2,257 条
│   ├── validation.json              # 验证集 266 条
│   └── test.json                    # 测试集 612 条
├── api_documentation_mapping.json   # API → RapidAPI 文档端点映射（已脱敏）
└── dataset_stats.json               # 统计摘要（样本/API/仓库/划分/对齐/脱敏等）
```

> 划分采用 **API 级**策略（同一 API 不跨集），杜绝接口泄漏。

---

## 快速开始

```bash
pip install -r requirements.txt

# ① 采集 RapidAPI 卡片（Node 版，需本机 Edge/Chrome）
node 01_rapidapi_crawler/rapidapi_search_cards_cdp.js --limit 3

# ② GitHub 代码爬取（先填好 token.json）
cp 02_github_code_crawler/token.json.example 02_github_code_crawler/token.json
# 编辑 token.json 填入自己的 GitHub Token
bash 02_github_code_crawler/start_crawler.sh

# ③ 合并、校验
python3 03_dataset_construction/merge_results.py
python3 03_dataset_construction/validate_final_dataset.py
python3 03_dataset_construction/audit_public_release.py

# ④ 主实验（以 DeepSeek 为例）
export DEEPSEEK_API_KEY=your_key
python3 04_experiments/main_experiment_pipeline.py split
python3 04_experiments/main_experiment_pipeline.py build-prompts --split test
python3 04_experiments/run_openai_compatible_model.py --model deepseek-chat \
    --prompts <prompts.jsonl> --output <out.jsonl>
```

> 提示：部分爬虫/构建脚本的默认输入输出路径源自原始开发环境，复现前请按需调整为你的本地路径。

---

## 仓库结构

```
.
├── 01_rapidapi_crawler/       # ① RapidAPI 端点 / 元数据采集
├── 02_github_code_crawler/    # ② GitHub 真实调用代码爬虫
├── 03_dataset_construction/   # ③ 清洗 / 合并 / 校验 / 脱敏意图
├── 04_experiments/            # ④ 主实验与自动评估
├── dataset/                   # 最终数据集 + 划分 + 统计
├── requirements.txt
├── LICENSE                    # 仓库源码的 MIT 许可证
├── DATASET_TERMS.md           # SCCGBench 数据集使用条款
├── README_zh.md               # 中文 README
└── README.md                  # 英文主 README
```

---

## 许可与数据使用

本仓库中的源码，包括 RapidAPI 爬虫、GitHub 代码爬虫、数据集构建脚本和实验评估脚本，采用 MIT License 发布，详见 [LICENSE](LICENSE)。

SCCGBench 数据集仅供学术研究使用。数据集中包含从公开 GitHub 仓库挖掘得到的服务调用代码片段，并与 RapidAPI 文档进行对齐。这些第三方代码片段的原始版权及原始许可证仍归对应仓库作者所有。SCCGBench 作者不对第三方代码片段进行重新授权。

所有嵌入的第三方真实凭证，包括 RapidAPI Key、OpenAI Key、Google API Key、GitHub Token 及其它密钥，均已脱敏并替换为 `<REDACTED_*>` 占位符。经全量扫描确认，发布数据集中无残留真实凭证。

使用者在使用数据集时，应自行遵守原始 GitHub 仓库的许可证和使用条款。使用者不得尝试恢复、推断或滥用任何已脱敏凭证，也不得在未经授权的情况下使用数据集访问付费、私有或受限 API。

详细数据集使用条款见 [DATASET_TERMS.md](DATASET_TERMS.md)。
