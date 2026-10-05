# shrag-rag —— 上交所年报 RAG 系统

针对 **100 家上交所上市公司 2025 年年度报告**（24224 页）构建的检索增强问答系统。

> **本仓库自包含。** 100 份年报 PDF（356 MB）与采集元数据已随仓库提交，
> 直接 `clone` 即可复现全部流程，不依赖外部项目。

与爬虫项目 [`shrag`](../shrag) 的关系：

| 项目 | 状态 | 职责 |
| --- | --- | --- |
| `shrag` | **已冻结**，不再维护 | 数据准备：爬取年报 PDF，产出 `annual_reports.csv` |
| `shrag-rag` | **唯一在维护的项目** | 解析 / 抽取 / 切片 / 索引 / 检索 / 问答 |

`scripts/01_parse.py` 会**优先读本仓库**的 `data/pdfs/` 与 `data/meta/`；
仅当本仓库数据缺失时，才回退到 `D:\Project\shrag`，方便两仓库并存时对照排查。

---

## 一、快速开始

```bash
git clone https://github.com/nannanness/shrag-rag.git
cd shrag-rag
pip install -r requirements.txt

# 需要环境变量 MINERU_API_KEY（MinerU 在线 API token，https://mineru.net）
python scripts/01_parse.py --limit 10      # 试点 10 家
python scripts/01_parse.py --limit 0       # 全量（已有产物自动跳过）
python scripts/02_inspect.py               # 验证解析质量并导出人眼比对文件
```

---

## 二、目录结构

```
shrag-rag/
├── shrag_rag/                       包（库，不含 IO）
│   └── parse/                       阶段1 解析
│       ├── schema.py                ParsedDoc / Block 统一数据模型
│       ├── mineru.py                MinerU 在线 API v4 客户端
│       ├── pdfsplit.py              分片 / 按页偏移合并
│       ├── htmltable.py             HTML 表格 → 二维网格
│       ├── export.py                Markdown / HTML 导出（人眼比对）
│       └── verify.py                内容驱动的质量验证
├── scripts/
│   ├── 01_parse.py                  编排：拆片 → 提交 → 轮询 → 合并 → 落盘
│   └── 02_inspect.py                验证 + 导出
├── tests/verify_merge.py            分片合并的页码偏移回归测试
├── diagnostics/                     排查过程留下的一次性脚本（后端对比等）
├── data/
│   ├── pdfs/<code>/*.pdf            ★ 年报原文（100 份 / 356 MB，随仓库提交）
│   ├── meta/annual_reports.csv      ★ 采集元数据（公司/年份/URL/文件大小）
│   ├── meta/companies.csv           全部上市公司名录（1604 家）
│   ├── meta/parse_manifest.csv      每份的页数/片数/块数/表格数/耗时
│   ├── meta/quality_report.csv      逐份质量验证结果
│   ├── meta/metrics_preview.csv     抽出的关键指标（阶段2 原型）
│   └── parsed/<code>/doc.json       ★ 归一化产物，下游只用这个（不入库）
└── logs/
```


---

## 三、为什么选 MinerU（实测对比）

朴素抽取（PyPDF2）能把文字抽出来，但**财务表格会烂**。同一张「主要会计数据」表：

```
朴素 PyPDF2:  |营业收入 4,241,548,459.664,191,787,662.411.19 4,005,115,417.62
              ↑ 4 个值粘成一个，列边界丢失

MinerU:       <td>营业收入</td><td>4,241,548,459.66</td>
              <td>4,191,787,662.41</td><td>1.19</td><td>4,005,115,417.62</td>
```

折行的行标签 `归属于上市公司股东的净利润` 也被正确还原成**单个单元格**
（朴素抽取会把它拆成两行并与数字分离）。

年报里表格极多 —— 实测 600108 有 **256 张表**，600004 有 **285 张**。
所以这不是边缘 case，而是主干路径。

---

## 四、踩坑记录

### 坑 1：MinerU 单文件 **200 页上限**

```
number of pages exceeds limit (200 pages), please split the file and try again
```

而本批年报页数**中位数 234**，实测 **75% 超限**：

| | 份数 |
| --- | --- |
| ≤200 页，可直接提交 | 25（25%） |
| >200 页，必须拆分 | **75（75%）** |
| 拆分后总请求数 | 177（原 100） |

所以 `pdfsplit.py` 是必需的，不是优化。默认按 **195 页**切片（留 5 页余量，
避免双方页数口径不同导致刚好被拒）。

**合并的关键点**：分片 `content_list.json` 的 `page_idx` 是**片内 0 基**，
合并回全文必须加该片首頁偏移，否则引用页码全错。`tests/verify_merge.py`
专门校验这一点（600004 拆 2 片后 `page_idx` 必须落在 `0~214`）。

**已知代价**：跨片边界的表格会被切成两半（MinerU 在单片内能跨页合并表格）。
每份最多影响边界处 1 张表，清单里的 `boundaries` 列记录了边界页供事后核对。

### 坑 2：老代码的两个误区（第 12 章那份）

| 老写法 | 问题 |
| --- | --- |
| 走 `extract/task`，要求 PDF 有**公网 URL** | v4 的 `file-urls/batch` **直接支持本地文件**，不必先传 OSS |
| 写死 `is_ocr: true` | 本批年报实测全是**文本型 PDF**，OCR 只会白白拉长耗时、消耗配额 |

### 坑 3：表格输出是 **HTML** 不是 Markdown

`full.md` 里的表格是 `<table><tr><td>` 形式，`content_list.json` 的
`table_body` 也是 HTML。**任何"按 `|` 切分"的代码都是错的。**

### 坑 4：页眉页码必须丢掉

`content_list.json` 把 `page_number`（每页 1 个）和 `header` 标成**独立类型**，
正好可以直接过滤。它们没有检索价值，却会污染切片 —— 朴素抽取会产出
`48/134二、财务报表` 这种粘连串。

---

## 五、`content_list.json` 是我们真正要的产物

MinerU 的 zip 里有多个文件，**只有 `content_list.json` 是结构化且带页码的**：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `*_content_list.json` | 2192 个块，每块带 `type`/`page_idx`/`bbox` | ★ 主数据源 |
| `*_content_list_v2.json` | 134 个块，按页分组，schema 不同 | 暂不用 |
| `full.md` | 拼好的 Markdown | 人工查看 |
| `layout.json` | 版面坐标 | 暂不用 |
| `images/` | 表格/图片切图（约 250 张/份） | 默认丢弃（体积约 20 倍） |

块类型分布（600108 实测）：`text 1667 / header 133 / page_number 134 / table 256 / image 2`。

**每个块都有 `page_idx`**，这是引用溯源的基础：
输出形如 `[亚盛集团2025年报 p.9 §三、经营情况讨论与分析]`。

---

## 六、后续阶段

- [x] 阶段 0：数据侦察（确认全文本型、无需 OCR）
- [x] 阶段 1：解析（MinerU + 分片合并）
- [ ] 阶段 2：结构化指标抽取 `metrics.parquet`（公司×指标×年份）★ 优先
- [ ] 阶段 3：章节感知切片（表格整块不切）
- [ ] 阶段 4：索引（DashScope `text-embedding-v4` + BM25）
- [ ] 阶段 5：检索（结构化路由 + 混合召回）
- [ ] 阶段 6：问答（带引用）
- [ ] 阶段 7：评测（用「主要会计数据」表自动生成带标准答案的问题集）

### 为什么一定要做结构化指标层

财报最典型的提问「**哪家净利润最高**」跟任何一段文本都不相似，
**向量检索在原理上答不了**。而年报「主要会计数据/主要财务指标」是
**强制标准化披露**的，行标签全国统一，抽出 `公司×指标×年份` 宽表后
这类问题变成精确查表/排序，零幻觉，还顺带成了评测集的 ground truth。
