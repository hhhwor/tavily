# 学术论文搜索工具调研与自建检索引擎可行性报告

| 项目 | 内容 |
|---|---|
| **报告日期** | 2026-06-11 |
| **调研定位** | 为类 Tavily（面向 LLM/AI Agent 的检索 API）产品增补一个**垂直领域**学术论文检索能力 |
| **核心诉求** | ① 数据覆盖广度与新鲜度；② 语义/向量检索 |
| **调研方法** | 多源并行检索 → 抓取 23 个来源 → 提取 104 条声明 → 对抗式核验 25 条（23 条确认、2 条驳回），结论以一手官方文档为主 |

---

## 一、执行摘要

**结论：自建可行，但正确形态是"混合模式"——以开放数据 dump 为底座自建检索/语义层，用实时 API 仅补新鲜度增量。** 纯全量镜像没必要，纯转调现成 API 又无法满足语义检索与垂直深度。

三个关键判断：

1. **底座现成且许可干净**：OpenAlex（CC0，~4.77 亿 works）与 Semantic Scholar Datasets（ODC-BY，~2 亿论文 + 24 亿引用边）两套开放语料即可支撑商用自建。
2. **语义检索可冷启动**：S2 直接提供 ~1.2 亿篇论文的**预计算 SPECTER2 向量**（Apache 2.0），无需自己跑 embedding；OpenAlex 已用 4.13 亿向量的 ES 索引证明生产级语义搜索（<250ms，~$1 万/月）可行。
3. **真正的瓶颈不是工程，而是**：①**数据再分发权**（摘要/全文常不可自由再分发）；②**新鲜度的边际成本**（免费 dump 有滞后，实时同步要花钱）。

---

## 二、调研背景与目标

目标是评估：在已有通用检索 API 之上，新增一个聚焦**单一垂直领域**的学术论文检索接口是否值得自建。约束为不做独立 ToC 学术网站、数据源可控、最看重覆盖新鲜度与语义排序。报告需给出可核验的量级/价格/许可数据，并产出从 MVP 到完整版的架构路径。

---

## 三、主流数据源与工具全景

### 3.1 元数据 / 批量 dump 数据源（核验数据）

| 数据源 | 量级 | License | 全量 dump | 新鲜度 | 定位 |
|---|---|---|---|---|---|
| **OpenAlex** | ~4.77 亿 works（2025 "Walden" 重写，18 个月内由 2.56 亿增长） | **CC0**（公共领域，商用+再分发无限制） | ✅ 免费季度全量，~330GB 压缩 / ~1.6TB 解压，gzipped JSONL | 免费快照季度更新；月度/每日变更需付费 Premium | ⭐ 首选底座，许可最干净 |
| **Semantic Scholar Datasets** | ~2 亿论文 + **24 亿引用边**；摘要 1 亿、作者 7500 万、tldr 5800 万、paper-id 4.5 亿 | **ODC-BY 1.0**（商用+再分发，需署名） | ✅ 批量下载，需免费 API key | 约每周刷新 | ⭐ 第二底座 + 现成引文图谱 + 现成向量 |
| **Crossref** | ~1.8 亿 DOI 记录（2026，+7.6% YoY），208GB 压缩 JSONL | 元数据多数可自由复用；**摘要有许可限制** | ✅ 免费 Public Data File | 年度文件 + 实时 API | DOI/期刊元数据交叉校准；无全文、无向量 |
| **arXiv** | 全文语料 ~9.2TB（2025-04，月增 ~100GB，2026 年中约 10.6TB） | ⚠️ 默认许可仅授予 arXiv **非独占分发权、不转让版权** | ⚠️ requester-pays S3（下载方付 AWS 带宽） | ⭐ 预印本最新鲜 | 元数据可索引/可 embedding，全文须**链回 arXiv**，默认许可不可自由再分发 |
| PubMed/Europe PMC、CORE、Unpaywall、DOAJ、Lens、Dimensions | 本次未逐一核验 | 各异 | 部分支持 | — | 按垂直域补充（如生物医学选 PubMed/Europe PMC；OA 全文补 CORE/Unpaywall） |

### 3.2 语义检索关键资产（对本项目最重要）

- **预计算向量**：S2 提供 `embeddings-specter_v2`——**~1.2 亿篇论文的 SPECTER2 向量**（30×28GB ≈ 840GB，**Apache 2.0**）。SPECTER2 为论文相似度专用文档级模型，向量可直接用于相关性排序。**这把"语义检索"冷启动成本大幅降低**。
- **生产级参考架构**：OpenAlex 自建 **4.13 亿 embeddings 的 Elasticsearch 向量索引**（含 1.97 亿 title-only 向量），**查询 <250ms**，服务成本 ~**$1 万/月**，但官方标注 **beta**。垂直域（数百万篇）成本可降至数量级更低（SPECTER2 约 768 维 ≈ 3KB/篇，500 万篇 ≈ 15GB 向量）。

### 3.3 面向用户的产品（形态参考，非数据源）

Google Scholar（覆盖最广、无 API）、Consensus/Elicit（LLM 综述问答）、Scite（引文断言）、Connected Papers/ResearchRabbit（引文图谱可视化）、Scopus/Web of Science（付费权威引文）。这些多数也构建在上述开放底座之上。

---

## 四、自建技术组件可行性

| 层 | 推荐方案 | 难度/成本 |
|---|---|---|
| 数据获取 | OpenAlex dump（CC0）+ S2 Datasets（ODC-BY）做底座；arXiv/OpenAlex/S2 实时 API 补增量 | 低，dump 免费、存储 TB 级 |
| 关键词检索 | Elasticsearch / OpenSearch（BM25） | 成熟，主要为运维 |
| 向量检索 | 复用 S2 SPECTER2 向量；Qdrant / Milvus / pgvector（垂直小数据量 pgvector 即可） | 中，可省自跑 embedding |
| 混合检索 | BM25 + 稠密向量 hybrid + cross-encoder 重排 | 中，质量关键 |
| 全文与解析 | 仅索引**真开放获取**：S2ORC（GROBID 解析，10M / v2 16M 条，ODC-BY）+ Unpaywall/CORE 补 OA PDF，自跑 GROBID | 中高，限定 OA 可绕开多数版权问题 |

> **更正**：S2ORC 全文覆盖**不是**传言的 "12M/136M"（已 0-3 驳回）；以核验过的 **10M / v2 16M 条**为准，且 S2ORC 须经 S2 API + 免费 key 获取，不再单独下载。

---

## 五、自建 vs 直接调用现成 API

| 维度 | 聚合开放 dump 自建 | 直接调现成 API |
|---|---|---|
| 语义/向量检索 | ✅ 完全可控可调 | ❌ 多数不给排序控制权 |
| 垂直域深度调优 | ✅ 可重排/加领域 embedding | ❌ 受限 |
| 覆盖广度 | ✅ dump 一次到位 | ✅ 但受 rate limit |
| **新鲜度** | ⚠️ 免费 dump 滞后（OpenAlex 季度、S2 周级） | ✅ 实时最新 |
| 成本 | 存储+索引+运维（垂直域可控） | 按量付费，规模化后上涨 |
| **法律风险** | ⚠️ 再分发摘要/全文是雷区 | ✅ 商责在供应商 |

**关键洞察**：最看重的两点分属两边——**语义检索须自建，新鲜度靠 API 最省心**。故混合是唯一合理解。

---

## 六、可行性结论

针对"**垂直领域 + 覆盖广度/新鲜度 + 语义检索 + 集成进现有搜索 API**"定位：

> **可行，推荐"开放 dump 底座 + 自建混合检索 + API 补增量"的混合架构。** 技术风险低（有现成向量与参考架构），主要工作量在数据管线与混合检索调优；**真正需要前置决策的是法务（再分发边界）与新鲜度成本（是否买 OpenAlex Premium）**。

---

## 七、推荐架构路径（MVP → 完整版）

**MVP（4–8 周，验证垂直域 + 语义检索）**

1. 选定单一垂直领域，从 OpenAlex dump 过滤该域子集（CC0，零法律负担）
2. 直接灌入 S2 的 SPECTER2 预计算向量 → pgvector/Qdrant
3. OpenSearch BM25 + 向量 hybrid 检索，封装为对齐 Tavily 风格的 `/academic/search`
4. 响应**只返回元数据 + 链接**（标题/作者/年份/DOI/OA 链接），暂不再分发摘要全文 → 规避版权

**V1（加深）**

5. 接入 OpenAlex/arXiv/S2 实时 API 做新鲜度增量（每日 poll 该域新论文）
6. 限定 OA 全文（S2ORC + Unpaywall）跑 GROBID，全文分块向量化，支持 RAG
7. 加 cross-encoder 重排 + 引文图谱信号（用 S2 的 24 亿引用边）

**V2（差异化/规模化）**

8. 评估付费 OpenAlex Premium（月度快照/每日变更）是否值得——取决于"最新预印本延迟"是否为卖点
9. 多垂直域扩展；SPECTER2 vs E5/BGE 做领域 A/B 或微调

---

## 八、风险与待澄清问题

1. ⚠️ **再分发边界（最高优先级，须法务确认）**：开放许可覆盖的是**元数据/编排与向量**，**不必然覆盖底层摘要与全文**。Crossref/S2 摘要、arXiv 默认许可全文可能不可再分发。API 若要吐摘要/snippet，须**逐条按 license gating**，上线前正式法律审查。
2. **OpenAlex 定价未定**：官方自述 $1/天免费额度等数字"仍在校准"，承诺同步架构前向 `sales@openalex.org` 核实当前 Premium 价格。
3. **数据量持续上涨**：OpenAlex 18 个月翻倍、arXiv 月增 100GB，容量规划留余量。
4. **新鲜度真实滞后未量化**：预印本从 arXiv 进入 OpenAlex/S2 的延迟需针对所选垂直域实测，以验证"覆盖广 + 新鲜"是否达标。

---

## 九、附录：数据来源与核验

**核验情况**：25 条声明经 3 票对抗式核验，23 条确认（多为一手官方文档，3-0 一致）、2 条驳回（OpenAlex 变更文件节奏的某措辞 1-2；S2ORC "12M/136M" 覆盖数 0-3，已排除）。

**主要一手来源**：

- [OpenAlex 数据快照文档](https://docs.openalex.org/download-all-data/openalex-snapshot) ｜ [OpenAlex 限额与定价](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication) ｜ [OpenAlex 定价博客](https://blog.openalex.org/openalex-api-new-features-and-usage-based-pricing/) ｜ [OpenAlex 向量搜索博客](https://blog.openalex.org/)
- [Semantic Scholar Datasets API](https://api.semanticscholar.org/api-docs/datasets) ｜ [allenai/s2orc](https://github.com/allenai/s2orc) ｜ [SPECTER2 介绍](https://allenai.org/blog/specter2-adapting-scientific-document-embeddings-to-multiple-fields-and-task-formats-c95686c06567)
- [Crossref 2026 Public Data File](https://www.crossref.org/blog/2026-public-data-file-now-available/) ｜ [arXiv 批量数据 S3](https://info.arxiv.org/help/bulk_data_s3.html) ｜ [GROBID 文档](https://grobid.readthedocs.io/en/latest/Introduction/)

**时效性提示**：本报告为 2026 年初快照，定价/数据量/许可条款均在快速变化，承诺架构前请直接向各数据源核实当前条款。
