# GitHub 代码爬虫

以 RapidAPI 端点信息为锚点，从 GitHub 抽取真实的 API 调用函数，并做质量打分、
工具生成代码检测与相似度去重。

## 目录

```
02_github_code_crawler/
├── config.py                       # 检索策略、质量阈值、注释加分、爬取参数
├── runtime_config.py               # 运行时输出路径与 token / API 列表加载
├── github_crawler_v3_enhanced.py   # 爬虫主程序
├── start_crawler.sh                # 启动脚本
├── token.json.example              # GitHub Token 模板
└── crawled_data/                   # 输出目录（运行时自动生成）
    ├── all_functions.json          # 爬取结果
    ├── statistics_report.json      # 统计报告
    └── .progress.json              # 断点续跑进度
```

## 使用

1. 准备 GitHub Token（建议多个以提高速率上限）：

   ```bash
   cp token.json.example token.json
   # 编辑 token.json，填入自己的 GitHub Personal Access Token 列表
   ```

2. 准备 RapidAPI 元数据（来自上一步 `01_rapidapi_crawler/`），放在
   `rapidapi_metadata/by_category/` 下。

3. 运行：

   ```bash
   bash start_crawler.sh
   # 或
   python github_crawler_v3_enhanced.py
   ```

4. 查看结果：

   ```bash
   python -c "import json; d=json.load(open('crawled_data/all_functions.json')); print('函数数:', len(d))"
   ```

## 检索策略

从精确到宽松分层检索（见 `config.py` 的 `QUERY_STRATEGIES`）：

```
endpoint URL 精确匹配 → route + host 匹配 → host 匹配
        → header 匹配 → 库匹配 (requests) → 函数定义匹配 → 关键词兜底
```

## 关键配置（`config.py`）

```python
MAX_FUNCTIONS_PER_API  = 20     # 每个 API 最多保留的函数数
MAX_FUNCTIONS_PER_REPO = 5      # 每个仓库最多保留的函数数（提高多样性）
MIN_QUALITY_SCORE      = 60.0   # 最低质量门槛
COMMENT_BONUS_PER_LINE = 2.0    # 注释加分（优先有注释的代码）
RATE_LIMIT_DELAY       = 8.0    # 搜索请求间隔（秒），过快会触发 429
```

## 质量评分

```
评分项：语法有效 30 · API 调用 25 · 响应处理 20 · 错误处理 10 ·
        代码长度 10 · 仓库质量 5 · 注释 15 · docstring 10
扣分项：数据库调用 -30 · 缩进问题 -20 · 缺少导入 -10
过滤：必须语法有效、含 API 调用、质量分 ≥ 60；排除自动生成代码与 85%+ 相似代码。
```

## 故障排除

- **频繁 Rate Limit**：增大 `RATE_LIMIT_DELAY` / `QUERY_DELAY`，或在 `token.json` 中添加更多 token。
- **SSL 错误**：多为临时网络问题，爬虫会自动重试。
- **速度慢**：在不触发限流的前提下适当降低延迟，或增加 token 数量。

## 许可

本项目用于学术研究，请遵守 GitHub API 使用条款。
