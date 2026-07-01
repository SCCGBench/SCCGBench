# 主实验代码说明

这部分代码只负责主实验流程，不会下载模型，也不会保存任何 API token。

## 1. 已覆盖的实验

- API-level 数据划分：同一个 `api_name` 不会同时出现在训练、验证和测试集中。
- API-grounded 代码生成主实验：默认使用 C4 上下文提示词。
- 上下文消融：C0 到 C4。
- Endpoint 选择实验：优先使用 RapidAPI metadata 构造同 API 候选 endpoints，`host_only` 样本默认跳过。
- 自动评估：Syntax、Host、Endpoint、Method、Header、Parameter F1、Response Handling、Error Handling、Mock Execution Pass。
- 结果汇总：把每个模型的评估汇总整理为 JSON/CSV 表格。

## 2. 主要脚本

```bash
python3 04_experiments/main_experiment_pipeline.py split
python3 04_experiments/main_experiment_pipeline.py build-prompts --split test
python3 04_experiments/run_openai_compatible_model.py --prompts "实验/提示词/API调用代码生成/test_主实验_C4.jsonl" --output "实验/模型输出/模型名/test_主实验_C4.jsonl" --model "模型名"
python3 04_experiments/main_experiment_pipeline.py evaluate --task codegen --prompts "实验/提示词/API调用代码生成/test_主实验_C4.jsonl" --outputs "实验/模型输出/模型名/test_主实验_C4.jsonl" --output "实验/自动评估结果/模型名/test_主实验_C4_评估.json"
```

## 3. 模型运行方式

`run_openai_compatible_model.py` 可用于两类模型：

- DeepSeek API：设置 `DEEPSEEK_API_KEY` 环境变量后运行。
- 本地开源模型：先用 vLLM、LMDeploy 或其他 OpenAI 兼容服务启动模型，再把 `--base-url` 指向本地服务。

API key 只从环境变量读取，不写入输出文件。

## 4. 输出要求

模型输出 JSONL 每条都会立即落盘，字段包括：

- `样本ID`
- `模型名称`
- `提示词`
- `原始输出`
- `调用时间`
- `temperature`
- `top_p`
- `max_tokens`
- `解析成功`
- `错误信息`
- `运行时间秒`

评估结果会保存解析后的代码和每个自动指标，最终报告中的数字应从这些文件重新计算。

## 5. 注意

- `host_only` 是弱对齐样本，不进入 endpoint 选择主实验。
- Parameter F1 优先使用 RapidAPI 文档参数；若文档参数缺失，则另外报告从原始调用代码中抽取到的 observed 参数。
- Mock Execution 不真实访问 RapidAPI，只拦截 `requests` 调用并检查请求构造。

## 6. 补充实验：稠密检索基线与 pass@k

### 稠密检索基线（`dense_retrieval_baseline.py`）
对应论文中稀疏（BM25）与稠密检索的对比。脚本从发布数据集构建端点语料（按 host+method+route 去重），用神经句向量编码意图与端点文档，按余弦相似度排序，并用与 BM25 相同的命中判定计算 Host@K / Endpoint@K。

```bash
pip install sentence-transformers torch
python dense_retrieval_baseline.py --model sentence-transformers/all-MiniLM-L6-v2
python dense_retrieval_baseline.py --model BAAI/bge-base-en-v1.5 \
    --query-prefix "Represent this sentence for searching relevant passages: "
```

结论：稠密检索显著优于 BM25（Endpoint@10 由约 34% 升至约 50–54%），但仍远低于 Oracle（约 89%），端点 grounding 仍是瓶颈。该自包含脚本基于发布数据集构建语料，数值与论文表同量级、结论一致；论文表的精确值由原始实验语料给出。

### pass@k（`passk_evaluation.py`）
对应“解码单一”的有效性威胁。对 COMMENT_ONLY 提示词以温度采样 n 次，逐次复用 Mock 评估，按无偏估计 pass@k = E[1 − C(n−c,k)/C(n,k)] 计算。

```bash
# 先生成提示词，并启动本地 Ollama（或任意 OpenAI 兼容服务）
python main_experiment_pipeline.py build-prompts --split test
python passk_evaluation.py --prompts <test_COMMENT_ONLY.jsonl> \
    --model qwen2.5-coder:7b --n 5 --temperature 0.8
```

结论：即使采样 5 次，pass@5 仍约 1.3%，与贪婪解码同量级，说明增大采样多样性无法突破可执行性瓶颈。

### 真实调用 sanity check（`real_call_sanity_check.py`）
对应论文中“Controlled real-call validation”段。该实验在不依赖真实网络与凭证的虚拟执行（Mock）之外，对 FREE RapidAPI 服务做小规模、安全过滤的真实调用，作为 Mock 指标的正向预测有效性（construct-validity）校验，**不替代主实验**。

安全约束：
- 仅使用 FREE 定价层、GET 方法、无副作用的 RapidAPI 端点
- `RAPIDAPI_KEY` 仅从环境变量读取，脚本中不保存、不输出
- 仅对 Mock 判定为正且目标 host 不变的请求发起真实调用
- 外部失败（认证/限流/网络/不可用）单独记录、不计入模型错误

```bash
# 1. 从测试集筛选 gold-reachable FREE API（dry run）
python real_call_sanity_check.py --limit 200 --dry-run

# 2. 跑虚拟 executor 捕获完整请求，只打印捕获信息
python real_call_sanity_check.py --limit 200 --complete-capture

# 3. 对捕获的请求做受控真实调用（需设置 RAPIDAPI_KEY 环境变量）
export RAPIDAPI_KEY=your_key
python real_call_sanity_check.py --replay /path/to/capture.json
```

汇总结果见 `strengthened_real_call_summary.json`。主要结论：Mock=1 请求的真实 2xx 率约 91%，且外部失败已分离，支持 Mock 作为可执行性 proxy 的正向预测有效性。Mock=0 的阴性对照较少（仅安全过滤范围内的 GET 请求），因此不声称 Mock 等价于真实执行。
