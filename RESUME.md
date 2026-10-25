# RESUME —— 续跑指引（新会话先读这一页）

> 本文件是**工作记忆的外部化**。上下文被压缩/新开会话时，读这一页即可恢复到
> 能继续干活的状态，不必重新探测。项目全貌见 [`README.md`](README.md)。

## 一句话现状

八个阶段**全部跑通**，100 家上交所 2025 年报已全量解析、抽取、切片、嵌入、
建好本地 FAISS 索引；检索 top-5 100%、top-1 80.6%；问答带引用可用。
现在处于「提质 + 补齐缺口」阶段。

## 当前 git 状态

```
仓库   D:\Project\shrag-rag      分支 master   远程 origin/master
状态   领先远程 1 个提交（a6adb60 未推出去）
已验证 5e22bfa 与 81aeac6 是推送成功的（reflog 里有 update by push）
待推   a6adb60  fix: 修好嵌入路径的鉴权 bug + 补全项目文档
```

**行动项**：网络可用时执行
`git -C D:\Project\shrag-rag push origin master`。
刚才失败是因为 `Recv failure: Connection was reset`（GitHub 连不上），
**不是代码或认证问题**。

**推送成功的判据**：GitHub 偶尔连不上，`push` 与 `ls-remote` 都可能失败，
所以别只看命令有没有报错。可靠的信号是
`git reflog show origin/master` 里出现 `update by push`，
以及 `git rev-list --left-right --count origin/master...master` 返回 `0  0`。

另一个仓库 `D:\Project\shrag`（爬虫）**已冻结**，HEAD `8124fd4`，不再改动。

## 立刻可跑的命令

```powershell
$py = 'D:\DevelopmentEnvironment\python\python.exe'

# 回归测试（必须 exit 0）
& $py D:\Project\shrag-rag\tests\test_spec.py
& $py D:\Project\shrag-rag\tests\verify_merge.py

# 检索评估（1090 题自动集，约 1 分钟）
& $py D:\Project\shrag-rag\scripts\08_eval.py
& $py D:\Project\shrag-rag\scripts\08_eval.py --metric 净利润 --show-fail 20

# 检索 / 问答
& $py D:\Project\shrag-rag\scripts\07_search.py -q "白云机场2025年营业收入" --compare
& $py D:\Project\shrag-rag\scripts\09_ask.py -q "哪家净利润最高" --evidence

# 全量嵌入（已有缓存，通常不需要；--rebuild-only 只重建 FAISS）
& $py D:\Project\shrag-rag\scripts\06_index.py --limit 0 --workers 8

# Web（本机 8000 端口；不要加 --reload，每次重载都重读 234 MB 索引）
& $py -X utf8 -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

## 数据在哪（体积 / 是否入库）

| 路径 | 内容 | 大小 | 入库 |
| --- | --- | --- | --- |
| `data/pdfs/<代码>/` | 100 份年报 PDF（每家一个子目录） | 356.3 MB | ✅ |
| `data/meta/` | 采集元数据、解析清单、质量报告 | 小 | ✅ |
| `data/metrics/` | `metrics_long.csv`(2828) / `metrics_wide.csv` / `extract_report.csv` | 小 | ✅ |
| `data/chunks/chunks.jsonl` | 59976 个 chunk | 122 MB | ❌ |
| `data/index/` | `vectors.faiss`(245.7 MB) + `id_map.jsonl` + 嵌入缓存 + `manifest.json` | 大 | ❌ |
| `data/parsed/` | 解析中间产物 | **5.8 GB** | ❌ |

`data/parsed/` **用户明确要求保留以便回溯复核，不要清理**。

## 关键数字（都是实测，不是估计）

| 项 | 值 |
| --- | --- |
| 公司 / 页数 / 表格 | 100 / 24224 / 29253 |
| chunk | 59976（文本 34226 / 表格 25750），中位 255 字 |
| 指标观测值 | 2828（2023:836 / 2024:807 / 2025:1185） |
| 拿满 12 指标的公司 | 90/100 |
| 检索 top-1 / top-5 | 879/1090 (80.6%) / **1090/1090 (100%)** |
| 嵌入成本 / 耗时 | 8.60 元（17.20 M tokens, 5428 次调用）/ 50.8 分钟 |
| 解析耗时 | 47 分钟（MinerU 免费额度内） |
| 嵌入模型 / 维度 | `text-embedding-v4` / 1024，batch **10（硬上限）** |
| LLM | `qwen-plus`（`qwen-turbo-latest` 会 403） |
| 融合公式 | `1.0×vec_norm + 0.3×bm25_norm + rerank_adj`（池内 min-max） |
| Web 冷启动 | 1.3 秒（索引 1.1s + 指标 0.01s）；BM25 后台预热 3 秒 |
| Web 常驻内存 | **1.33 GB**（chunks 0.54G + BM25 0.76G + faiss 0.23G） |
| Web 单问 | 首帧 meta 1.5 秒，首字 2.2 秒，整题 5 秒左右 |

## 环境（沙箱相关，容易忘）

- **工作目录** `D:\Project\dsDemo`（沙箱根）；改 `D:\Project\shrag-rag` 下的
  **已有文件**需要 `danger-full-access` + justification。
- 新文件可以在 `D:\Project\dsDemo\_staging\shrag-rag\` 起草，
  **但已有文件一律直接改项目里的那份**（拷贝回项目曾把改好的文件覆盖回去）。
- Python：`D:\DevelopmentEnvironment\python\python.exe`（3.13，无 `Path.is_ascii()`）。
- 密钥只在 **User 作用域**：`MINERU_API_KEY`、`DASHSCOPE_API_KEY`，
  用 `[Environment]::GetEnvironmentVariable('NAME','User')` 读。
- 中文提交信息：写到临时文件再 `git commit -F`。
- 代码文件不要带 BOM（`Set-Content -Encoding UTF8` 会加）；
  **CSV 的 `utf-8-sig` BOM 是故意的**（Excel 中文）。

## 已知缺口 / 未验证项（别当成已完成）

1. 银行类年报指标覆盖低（民生 6/12、招商 7/12），模板差异，非 bug
2. `metrics_wide.csv` **已知仍有少量错值**，只用于评测与排序，不当权威数据引用
3. `净利润` 的 100/100 含**口径假设**（模板只披露归母时用归母兜底）
4. `净利润` 观测数比 `归母净利润` 少 53 条 —— 未逐份核实原因
5. 表格 chunk 仅 6% 关联到财务指标
6. 超长表格按 12000 字截断（理想做法换摘要）
7. top-1 80.6% 未继续调 —— top-5 已 100%，再调是同批数据过拟合
8. 公司简称（「浦发」）与集合称谓（「四大行」）认不出
9. **`扣非加权平均净资产收益率` 没进自动评测** —— 被 `08_eval.py:31` 的 `SKIP`
   排除（缺出题模板），这 95 家能否检索到**没有实测证据**
10. **报表兜底抽取有单位标错（覆盖率 100% 不等于正确）** —— 1185 个单元格里
    17 个量级明显不对：北方稀土营业收入 `29.11 元`、杭钢股份营业收入 `-17.81 元`、
    保利发展净利润 `5,022,154,432.21 万元`（数值其实是元，放大一万倍）。
    目前只在 Web 展示层标「?」，根因在 `declared_unit` 的判定，**下一步该修这里**
11. **净利润是唯一没有标准表来源的指标**（98 条全 statement），排序靠兜底值，
    已加单位一致性校验并让 prompt 声明，但不如标准表可靠
12. Web 无鉴权，只适合本机

## 最近一次改动（Web 这一轮）

提交 `0507ade`（检索提速）、`77e35ed`（流式 + 排序校验）、`3325012`（Web 界面）。

- **路由**：候选集原来每次遍历 6 万 chunk，带「公司+指标」要 600 多毫秒；
  改成初始化时建「公司→行号」「指标→行号」两张表取交集。全量评测确认
  结果不变（879/1090、top-5 100%）
- **流式**：`DashScopeLLM.chat_stream()` + `RagQA.stream()` 吐
  meta/delta/done。首字从 1813 ms 降到 347 ms（实测单次 LLM 调用）
- **排序缺陷（重要）**：`净利润` 98 条全是 `statement` 来源，而
  `lookup_ranking` 默认只收 `summary`，于是「哪家净利润最高」的榜单**本来是空的**。
  现在标准表为空时退回兜底来源并标 `degraded`；同时加了单位一致性校验，
  剔掉了保利发展那条被放大一万倍的净利润。修复后第一名是招商银行
- **Web**：`python -m uvicorn web.app:app --port 8000`。
  索引只加载一次（1.33 GB），不要开多 worker、不要 `--reload`
- **数据问题**：做 Web 时查出 17 个单元格单位标错（详见已知缺口第 10 条），
  目前只在展示层标「?」，**根因还没修**

## 下一步候选（按性价比）

| 方向 | 收益 | 成本 |
| --- | --- | --- |
| 修 `declared_unit`，消掉那 17 个单位标错 | 数值可信度 | 中 |
| 补 `扣非加权平均净资产收益率` 出题模板纳入评测 | 12 指标全部有实测 | 极低 |
| 补 5 家扣非 ROE + 3 家稀释 EPS | 95%→100% 覆盖 | 低 |
| 简称/集合称谓别名表 | 口语问法可答 | 低 |
| 长表格改摘要而非截断 | 大表信息不丢 | 中 |
| 追问式多轮问答 | 交互体验 | 中 |

## 踩坑清单

完整清单在 README 的「踩过的坑」一节，按采集、MinerU、FAISS、解析、抽取、检索、评测、工程分组。
改代码前先扫一遍。最耗时的几条：

- FAISS 向量不归一化，得分能到 7.543，错误结果反超
- faiss 写中文文件名不报错但写乱码名
- MinerU 把所有标题标成同一级，层级得自己按编号推
- 金额单位是文档级声明且会被局部表格覆盖，不处理会差 100 倍
- `_fill_from_statements` 里一个 `break` 位置错了，每表只记一个指标，出系统性错值
- 加分规则重复计数会把无关章节顶到第一
- 评测用字符串比数值，把对的判成错的
- 鉴权失败要追问「key 传给了谁」，死代码 `ds` 属性让 key 读了却没生效，
  症状是 `AuthenticationError` 而不是「没找到 key」
- 跑 `08_eval.py` 看汇总行而不是退出码，jieba 的 stderr 会让退出码非 0
