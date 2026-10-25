# shrag-rag

上交所 100 家上市公司 2025 年年报的检索问答系统，语料 24224 页。

仓库自带 100 份 PDF（356 MB）和采集元数据，clone 下来能跑完整流程。采集代码在另一个仓库
`shrag`，已冻结。

## 状态

八个阶段都已跑通，全量数据入库，另有一个本机 Web 界面。

| 阶段 | 结果 |
| --- | --- |
| 采集 | 100 家 2025 年报 |
| 解析 | MinerU，100/100 |
| 抽取 | 2828 个指标观测值 |
| 切片 | 59976 个 chunk |
| 索引 | text-embedding-v4 + FAISS，全量完成 |
| 检索 | top-1 80.6%，top-5 100% |
| 问答 | 带引用，支持跨公司排序 |
| 评测 | 1090 题，从指标表反推生成 |

## 用法

```bash
git clone https://github.com/nannanness/shrag-rag.git
cd shrag-rag
pip install -r requirements.txt
```

两个 key，放 User 级环境变量即可（脚本读不到进程变量会再去 User 作用域取）：

```
MINERU_API_KEY     解析        https://mineru.net
DASHSCOPE_API_KEY  嵌入和问答   https://bailian.aliyun.com
```

```bash
python scripts/06_index.py --limit 10           # 先建 10 家试试，约 1 元
python scripts/09_ask.py -q "白云机场2025年的营业收入是多少"
python scripts/08_eval.py                       # 自动评估检索
```

其余常用命令：

```bash
python scripts/06_index.py --limit 0 --workers 8          # 全量嵌入，约 51 分钟
python scripts/06_index.py --rebuild-only                 # 只重建 FAISS
python scripts/07_search.py -q "..." --compare            # 检索消融对比
python scripts/08_eval.py --metric 净利润 --show-fail 20
python scripts/09_ask.py -q "哪家净利润最高" --evidence
python tests/test_spec.py && python tests/verify_merge.py # 回归测试
```

## Web 界面

```bash
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 ，两个页签：问答和指标浏览。

索引常驻约 1.3 GB，启动时只加载一次，所以**不要开多 worker，也不要用 `--reload`**
（每次热重载都要重读 234 MB 的 faiss 索引）。实测冷启动 1.3 秒，首帧 1.5 秒，
首字 2.2 秒，整题 5 秒左右。

## 结构

```
shrag_rag/          纯逻辑，不含 IO，可单独 import
  spec.py           指标口径和文本工具
  parse/            解析
  extract/          指标抽取
  chunk/            切片
  index/            嵌入和向量库
  retrieve/         检索和重排
  answer/           问答和引用
scripts/            流程编排，01 到 09
web/                HTTP 服务与静态页面
tests/              test_spec.py、verify_merge.py
data/
  pdfs/<代码>/      100 份年报
  meta/             采集元数据、解析清单、质量报告
  metrics/          结构化指标表
```

## 数据

| 项 | 数值 |
| --- | --- |
| 公司 / 页数 | 100 / 24224 |
| PDF 体积 | 356.3 MB |
| 表格 | 29253 张 |
| chunk | 59976（文本 34226，表格 25750） |
| chunk 长度 | 中位 255，P75 518，P90 957 |
| 指标观测值 | 2828（2023 年 836，2024 年 807，2025 年 1185） |

2025 年的指标覆盖：

| 指标 | 覆盖 |
| --- | --- |
| 营业收入、利润总额、归母净利润、总资产、归母净资产、经营现金流净额 | 100/100 |
| 加权平均净资产收益率、基本每股收益 | 99/100 |
| 净利润 | 98/100 |
| 稀释每股收益、扣非归母净利润 | 97/100 |
| 扣非加权平均净资产收益率 | 95/100 |

90 家拿满 12 个指标，其余差 1 到 3 个，主要是银行（民生 6/12、招商 7/12），属模板差异。
净利润的 100/100 是算出来的：模板有时候只披露归母那一行，取归母值兜底，属于口径假设。

检索评估用 1090 道自动生成的题，标准答案取自指标表：

| 方案 | top-1 | top-5 |
| --- | --- | --- |
| 纯向量（10 家手写集） | 3/10 | 6/10 |
| 向量 + BM25（无路由无重排） | 7/10 | 9/10 |
| 完整（路由 + 重排，10 家） | 10/10 | 10/10 |
| 完整（100 家，1090 题） | 879/1090 (80.6%) | 1090/1090 (100%) |

100 家规模没有衰减。生成阶段看的是 top-5，所以 top-5 全中比 top-1 更关键。
扣非加权平均净资产收益率缺出题模板，评测集覆盖 11 个指标。

成本：解析 100 份 47 分钟（MinerU 免费额度内），嵌入全量 8.60 元 / 50.8 分钟
（17.20 M tokens，5428 次调用）。

## 设计

层与层之间只有两个数据结构。

```python
Block(kind, page_idx, text, html, bbox, ...)      # 解析层输出
kind ∈ {text, heading, table, image}

MetricValue(code, name, metric, year, value, unit, label, page, source)
```

页眉页脚在解析层丢掉，表格保留 HTML。这一层不暴露 MinerU 细节，换解析器只要加 adapter。
`MetricValue.unit` 是金额单位，排序和展示都要用它。

### 双通道

数值类问题走结构化通道，文本类走检索：

| 问题 | 走哪条 |
| --- | --- |
| 亚盛集团 2025 年营业收入 | 结构化，注入已核实值 |
| 哪家净利润最高 | 结构化，查表排序 |
| 华能国际的审计机构 | 文本，向量 + BM25 |
| 浦发银行的经营策略重点 | 文本 |
| 净利润为什么下降 40% | 两路联合 |

结构化通道依赖年报的「主要会计数据 / 主要财务指标」表：行标签全国统一，列就是年份，
一张表给出三年数据。这张表同时也是评测集的标准答案。

### 关键参数

| 位置 | 参数 | 值 |
| --- | --- | --- |
| `chunk/chunker.py` | `FLUSH_LEVEL` | 2 |
| `answer/qa.py` | 融合权重 | 1.0 × 向量 + 0.3 × BM25 + 重排 |
| `retrieve/rerank.py` | 加分规则 | 只认首个命中 |
| `pdfsplit.py` | `DEFAULT_MAX_PAGES` | 195 |
| `index/embedder.py` | 并发 / 批大小 | 8 / 10 |

## 已知问题

1. 银行类年报指标覆盖偏低（民生 6/12、招商 7/12），模板差异，不是 bug
2. 报表兜底抽取存在单位标错：1185 个单元格里 17 个量级明显不对，例如北方稀土营业收入抽成
   `29.11 元`、保利发展净利润 `5,022,154,432.21 万元`（数值其实是元）。覆盖率 100% 不等于
   正确，目前只在界面上标「?」提示核对原文。`metrics_wide.csv` 同样只能用于评测和排序，
   不要当权威数据引用
3. `data/parsed/` 占 5.8 GB，`scripts/04_prune.py` 能清到 316 MB（默认 dry-run，未执行）
4. 表格 chunk 只有 6% 关联到财务指标，其余是业务类表格
5. 超长表格按 12000 字截断，理想做法是换摘要
6. top-1 是 80.6%，没有继续调：top-5 已经 100%，在同一批数据上再调只会过拟合
7. 公司名匹配不认简称与集合称谓，「浦发」「四大行」这类问法答不了
8. 净利润是唯一没有标准表来源的指标（98 条全是报表兜底），排序时需带来源说明
9. Web 端没有鉴权，只适合本机使用

## 版本控制

入库：`shrag_rag/`、`scripts/`、`web/`、`tests/`、`diagnostics/`、`configs/`、
`data/pdfs/`、`data/meta/`、`data/metrics/`。

不入库：`data/parsed/`（5.8 GB）、`data/index/`、`data/chunks/`（121.8 MB）、`logs/`。

`data/index/manifest.json` 存了 `chunks.jsonl` 的 sha256 前 16 位，用于检测漂移：
切片重跑过而索引没重建时，哈希对不上会报错。
