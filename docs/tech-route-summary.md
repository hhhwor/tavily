# 目前技术路线总结(当前实现快照)

> 对象:本项目(面向 AI Agent / LLM 的通用 Web 搜索引擎,Tavily 路线)**当前已落地的真实实现**。
> 一句话:**元搜索聚合**——包装现成搜索源 + 跨源去重/融合 + cross-encoder 段落级重排,以结构化 JSON 返回 LLM-ready 内容,**不自建全网爬虫与倒排索引**。另接入 **OpenAlex 学术检索(经本地 Chukonu 检索系统)** 与 **专利检索(houdutech 只读 ES)** 作为两条独立能力支线(学术论文 / 专利结果各自单独成块)。
> 代码:[src/](../src/) ｜ 调研与选型对比:[agent-search-engine-tech-research.md](./agent-search-engine-tech-research.md) ｜ 学术引擎可行性:[academic-search-engine-feasibility.md](./academic-search-engine-feasibility.md) ｜ 评测体系:[eval-methodology.md](./eval-methodology.md)
> 编写日期:2026-06-10 ｜ 更新:2026-06-11(接入 OpenAlex 学术检索 + provider 召回缓存)｜ 2026-06-16(接入专利检索 ES 支线)｜ 2026-06-17(专利源切换到 epo_docdb_v2 多语种索引;引擎升级 Python 3.11 + FastAPI 进程内挂载 MCP server `/mcp`)｜ 2026-06-21(专利索引切到读别名 `epo_docdb_read`→`epo_docdb_v2_20260620`)｜ 2026-07-17(统一 Ranking Profile、建立 composition root、拆分应用服务)

本篇只讲**现在到底怎么做的**:分层、选型、默认开关、评测结论、运行方式。选型「为什么这么选 / 和谁对比过」见调研文档;评测「指标怎么算」见评测文档。

---

## 1. 路线定位

Agent 需要的不是给人导航的 SERP(标题+链接),而是**正文内容本身**的结构化数据,直接喂给 LLM。因此整条管线的目标是:**raw web → LLM-ready content**,在单次 API 调用内完成,核心价值在**聚合 + 重排 + 清洗**这条后处理管线,而非「真搜索引擎」的爬全网/存 PB/反爬/排序。

落地路线的三条务实取舍:

1. **不自建抓取层** —— 选用**自带正文摘要**的搜索源(腾讯/百度),省掉独立的 L2 正文抓取。
2. **重排 API 化** —— 用 SiliconFlow 云端 BGE 重排,零 GPU 依赖,延迟从本地 CPU 的数十秒降到 ~2.7s。
3. **只做文本** —— 不碰视频/音频/图片,换取速度与 LLM 契合度。

---

## 2. 端到端数据流

```
POST /search {query, top_k, ranking_profile?, rerank_threshold_mode?, ...}
      │
      ▼
 L0 查询理解  ── NFKC 规范化 + 时效识别(day/week/month/year)+ 学术意图识别 + 专利意图识别 + 输入校验
      │          (可选)LLM 改写:口语化 → 检索关键词,LRU+TTL 缓存,默认关
      │  产出 SearchPlan{normalized_query, rewritten_query, recency, time_sensitive, academic, patent, providers}
      ▼
 L1 多源并发检索(ThreadPoolExecutor) ── web 源 + (命中学术意图时)OpenAlex + (命中专利意图时)专利 ES 同池并发
      │   ★ 召回缓存:命中则跳过该源 API 调用(时效查询 time_sensitive 不缓存)
      │   web: 腾讯 SearchPro · 百度千帆 · SerpAPI(按 .env 凭证自动启用)
      │   学术: OpenAlex /works(自带 relevance_score;摘要倒排索引重建为正文)
      │   专利: houdutech 只读 ES /{index}/_search(multi_match patent_name^3/abstract^2/title_zh^2/abstract_zh;中英文皆可)
      │   时效下传各源原生过滤(腾讯 FromTime/ToTime · 百度 search_recency_filter
      │        · SerpAPI tbs · OpenAlex from_publication_date · 专利 ES range(application_date))
      │   记录 provider_rank(源内排名,供 RRF)
      ▼
 L2 正文抓取  ── 省略(腾讯/百度直接返回正文;SerpAPI 用 snippet 充当;OpenAlex 用摘要;专利用摘要)
      │
      ▼
 L3 + L4 跨源处理(由 RANKING_PROFILE 决定) ── web/学术/专利三路独立排序
   ├─ quality(默认):cross-encoder 文本分 + 各领域辅助信号
   ├─ semantic:仅 cross-encoder 文本相关性,辅助特征权重为 0
   └─ fast:不调用文本模型;Web 走 RRF,学术/专利按来源原始分
      │   文本阈值模式:off=关闭;prefer=达标项优先并回填;strict=硬过滤
      ▼
 L5 摘要/合成  ── 未做(留 hook)
      │
      ▼
 归一化 JSON {query, normalized_query, rewritten_query, recency, time_sensitive,
              partial_failure, failures:[{stage,source,type,code,message,recoverable}],
              answerability:{status,confidence,gaps:[{code,severity,message,type,source}]},
              evidence:[{id,result_id,type,source,title,url,published_date,
                         passage:{text,snippet_type,char_start,char_end,...},
                         citation:{label,authors,year,venue,doi,work_id,publication_number},
                         scores:{relevance,source_rank,rerank_score,authority,confidence},
                         access:{is_open,license,oa_pdf_url,pdf_status,next_cursor},
                         diagnostics:{warnings,partial,failure_code}}],
              count, providers_used, reranker,
              ranking_profile, rerank_threshold, rerank_threshold_mode, ranking_warnings,
              elapsed_ms}
```

---

## 3. 分层技术栈(已落地)

| 层 | 实际选型 | 代码 | 状态 |
|----|---------|------|------|
| **API** | FastAPI 应用工厂:`POST /search` · `GET /health` · `GET /`;导入无配置/模型副作用 | [api.py](../src/api.py) · [static/index.html](../src/static/index.html) | ✅ |
| **组合根/生命周期** | 冻结 `Settings.from_env()` + `Container`;lifespan 统一创建/关闭 Engine、HTTP Session、Executor、MCP | [bootstrap.py](../src/bootstrap.py) · [config.py](../src/config.py) | ✅ |
| **应用用例** | `SearchCommand` + `SearchService`;规划、召回、排序、PDF、Evidence、Trust、Answerability 通过阶段 Outcome 协作 | [application/](../src/application/) · [engine.py](../src/engine.py) | ✅ |
| **L0 查询理解** | 规则版(NFKC 规范化 + 时效识别 + 学术意图识别 + 专利意图识别 + 输入校验)+ 可选 LLM 改写(SiliconFlow Qwen2.5-7B,LRU+TTL 缓存) | [l0.py](../src/l0.py) | ✅ 规则;LLM 改写默认关 |
| **L1 搜索源** | 腾讯 SearchPro + 百度千帆 + SerpAPI,凭证驱动自动启用,并发检索 | [providers/](../src/providers/) · [application/recall.py](../src/application/recall.py) | ✅ |
| **L1′ 学术源** | OpenAlex `/works`(独立能力支线;摘要倒排索引重建;凭证驱动启用) | [providers/openalex.py](../src/providers/openalex.py) | ✅ 需 `OPENALEX_API_KEY` |
| **L1″ 专利源** | houdutech 只读 ES `/{index}/_search`(独立能力支线;multi_match 中文检索;URL 驱动启用) | [providers/patent_es.py](../src/providers/patent_es.py) | ✅ 需 `PATENT_ES_URL` |
| **L2 抓取** | 省略(源自带正文) | — | ⏸ 不需要 |
| **L3 去重/分块** | URL 归一化跨源去重 + 文档分块(段落→句子→字符三级,合并短段+重叠) | [dedup.py](../src/pipeline/dedup.py) · [chunk.py](../src/pipeline/chunk.py) | ✅ |
| **L4 重排** | `quality/semantic/fast` 三档;SiliconFlow API(BGE-v2-m3)/本地 BGE/FlashRank;`off/prefer/strict` 阈值策略;**web / 学术 / 专利三路独立重排** | [ranking_options.py](../src/pipeline/ranking_options.py) · [rerank.py](../src/pipeline/rerank.py) · [fusion.py](../src/pipeline/fusion.py) | ✅ |
| **L5 合成** | 未做 | — | ⏳ TODO |
| **缓存** | provider 召回级缓存(进程内 LRU+TTL,线程安全;接口化预留 Redis);时效查询不缓存 | [cache.py](../src/cache.py) · [application/recall.py](../src/application/recall.py) | ✅ |
| **横切**(安全/可观测/MCP) | 未做 | — | ⏳ TODO |

---

## 4. 各层实现要点

### L0 — 查询理解([l0.py](../src/l0.py))

- **规范化** `normalize()`:NFKC 全角→半角、压缩空白、去首尾标点;不改动中文正文。
- **时效识别** `detect_recency()`:正则规则把「今天/本周/本月/今年/最新…」映射到 `day/week/month/year` bucket(顺序敏感,先具体后泛化;泛化的「最新/最近」默认 `month`)。再加 `\b20\d{2}\b` 年份探测 → `time_sensitive` 标记。
- **学术意图识别** `detect_academic()`:正则规则(`论文/文献/综述/预印本/arxiv/survey/citation/paper/...`,英文用词边界避免 `newspaper` 误命中 `paper`)判定是否触发 OpenAlex 学术检索 → `academic` 标记。`force_academic`(来自 API 的 `include_academic`)可显式覆盖:`None`=自动 / `True`=强制开 / `False`=强制关。
- **输入校验**:空查询报错;超 `MAX_QUERY_LEN=512` 截断,防滥用。
- **LLM 改写**(可选,默认关):口语化 → 检索关键词,走 SiliconFlow `Qwen2.5-7B-Instruct`,5s 超时,失败回退原查询;`_RewriteCache`(LRU + 1h TTL)命中时零延迟。产出 `SearchPlan`,引擎用 `rewritten_query or normalized_query` 去检索。

### L1 — 搜索源([providers/](../src/providers/))

统一接口 `SearchProvider.search(query, top_k, recency) -> List[SearchResult]`,各源把时效 bucket 映射到自己的原生过滤参数:

| 源 | 鉴权 | 正文 | 时效参数 | 关键约束 |
|----|------|------|---------|---------|
| **腾讯 SearchPro** | TC3-HMAC-SHA256(SecretId+Key,纯标准库) | ✅ `content`/`passage`/`score` | `FromTime`/`ToTime`(Unix 时间戳) | 中文强、英文弱 |
| **百度千帆** | Bearer(单 key) | ✅ `content` | `search_recency_filter`(week/month/year 枚举) | 查询限 **72 字符**(汉字算 2),`trim_query()` 先剥口语前缀再硬截断 |
| **SerpAPI** | api_key 查询参数 | ❌ 仅 snippet(用 snippet 充当 content) | Google `tbs`(qdr:d/w/m/y) | 100 次/月免费;补英文/全球覆盖 |

启用逻辑(`settings.enabled_providers`):**有哪家凭证就启用哪家**,无需改代码。`RecallCoordinator` 使用组合根注入的共享 Executor 并发查询,每条结果记 `provider_rank`(源内 0-based 排名)。

### L3 — 去重 + 分块

- **去重** `dedup()`:URL 归一化(去 scheme/www、小写 host、去末尾斜杠、剔除 utm_/spm/from/ref/source 等跟踪参数)后按 URL 合并,**保留正文更长的一条**并合并来源标记(`tencent+baidu`)。
- **分块** `chunk_text()`:段落(`\n\n`)→ 句子(中英标点)→ 字符三级拆分,合并相邻短段到接近 `max_chars=400`,相邻 chunk 留 `overlap=50` 字符重叠。供段落级重排使用。

### L4 — 重排(质量分水岭)([rerank.py](../src/pipeline/rerank.py))

生产链先把新旧请求字段解析成一个不可变 `RankingOptions(profile, threshold, threshold_mode)`，再由三类领域 reranker 执行。旧 `ThresholdReranker` / `FusionReranker` 仅保留给历史评测代码，不再是生产构建链。

- **段落级打分**:每个文档切 chunk,逐 `(query, chunk)` pair 交 cross-encoder 打分,**每文档取 chunk 最高分(max-pooling)**。彻底解决「BGE 512 token 上限 vs 长正文硬截断」的矛盾。
- **三种 backend**:
  - `siliconflow`(默认)—— 云端 BGE-v2-m3,无需 GPU;当前实现不再假设 25 文档硬上限,返回分数已在 0–1。
  - `bge` —— 本地 `sentence-transformers` CrossEncoder,需 torch。
  - `flashrank` —— 轻量本地 cross-encoder。
- **三种 Profile**:`quality` 为当前默认，文本分与 Web RRF、学术 citations/freshness/venue/OA、专利来源分/freshness/citations/status 融合；`semantic` 只使用文本分；`fast` 完全不调用文本模型，Web 使用 RRF，垂直源按原始分排序。
- **归一化 + 阈值**:本地 backend 用 sigmoid 归一化到 0–1；阈值判断的是**领域融合前文本分**。`off` 不应用阈值，`prefer`（默认）让达标项优先且在不足 `top_k` 时回填低分项，`strict` 才真正丢弃低分项。
- **NoOp 降级**:使用 `fast` 或 scorer 不可用而降级为 NoOp 时，文本阈值自动关闭并记录 `THRESHOLD_SKIPPED_NO_SCORER`；这属于预期降级诊断，不构成 partial failure。
- **RRF**(`fusion.py`):Web fast 路径使用 `rrf_fuse()`,`score = Σ 1/(60+rank)`,只看源内排名、与各源不一致的绝对分数无关,天然实现多源共识加权。

### L1′ — 学术检索(OpenAlex 数据,经 Chukonu 服务)([providers/openalex.py](../src/providers/openalex.py))

把 OpenAlex 论文作为**独立能力支线**接入:内部与 web 分开召回/重排,对外统一转成 `type=academic` 的 evidence 并与 web/patent 按相关性混排。**数据源(2026-06-21 起)= 本地 Chukonu 检索系统**(`http://localhost:9001`)的 ES,而非直连公网 `api.openalex.org`。选型与可行性见 [academic-search-engine-feasibility.md](./academic-search-engine-feasibility.md)。

- **触发**:`L0 学术意图识别` 命中(或 `include_academic=true` 强制)且学术源已启用时,OpenAlex 与 web 源**同一线程池并发召回**,不阻塞 web 主流程。
- **召回**:`POST {OPENALEX_API_URL}/openalex/search/keyword`,body `{query, size, year_min/year_max?}`;服务端在 `title^3/abstract^2/authors/concepts` 上 `best_fields` 检索。时效(recency)近似映射为 `year_min=year_max=当年`。可选 `X-API-Key`(服务未配 `SE4AI_API_KEYS` 时全部放行)。
- **摘要**:Chukonu 服务已把 `abstract_inverted_index` 重建为正文(`abstract` 字段直接可用),provider 无需再还原。
- **语义排序复用现有重排**:`AcademicResult` 设计为 `SearchResult` 子类,天然带 `text_for_rerank()`(返回「标题+摘要」),**现有 cross-encoder reranker 零改动**即可对 query↔论文打分;web 与学术两路独立重排、并发执行。`fast` 时按服务 `_score` 排序。
- **学术 evidence 元数据**:对外返回 `type=academic`。标题/URL/年份进入顶层字段;作者/年份/期刊/DOI/OpenAlex work_id 进入 `citation`;开放获取、license、`oa_pdf_url`、PDF 抽取状态和 `next_cursor` 进入 `access`;正文优先用 PDF 抽取文本(`snippet_type=pdf_text`),否则用摘要(`snippet_type=abstract`)。
- **启用**:配了 `OPENALEX_API_URL`(默认 `http://localhost:9001`)即启用;服务不可达时静默返回空,web 搜索零影响。
- ⚠️ **覆盖收窄**:Chukonu 当前 ES 仅 **5 万条 OpenAlex 子集**(非公网全量 ~2.4 亿 works),冷门主题召回会明显变弱;换公网全量需把 `OPENALEX_API_URL` 指回自建/官方全量服务(provider 协议一致)。
- ⚠️ **中文学术查询召回偏弱**:OpenAlex 以英文文献为主,中文 query 易被字面误解;引擎已默认对学术 query 做中→英改写(`OPENALEX_QUERY_REWRITE`)缓解。**英文学术查询效果显著更好**。

### L1″ — 专利检索(houdutech 只读 ES)([providers/patent_es.py](../src/providers/patent_es.py))

把 houdutech 演示集群的**只读专利 ES**(`https://search.houdutech.cn:9243`)作为**第二条独立能力支线**接入:内部与 web/学术分开召回/重排,对外统一转成 `type=patent` 的 evidence 并混排。架构定位与 OpenAlex 学术支线完全平行(意图触发 + 复用现有重排)。默认索引用**读别名 `epo_docdb_read`**(当前指向 `epo_docdb_v2_20260620`,EPO DOCDB,~1.72亿,**全球多语种**)——用别名而非固定版本号,集群蓝绿切换索引时本侧零改动。

- **触发**:`L0 专利意图识别` 命中(`detect_patent`:专利/发明专利/实用新型/外观设计/公开号/申请号/patent/IPC 等)或 `include_patent=true` 强制,且专利源已启用时,专利 ES 与 web/学术源**同一线程池并发召回**。
- **检索**:`POST {base}/{index}/_search`,构造 ES Query DSL —— `multi_match` over `patent_name^3 / abstract^2 / title_zh^2 / abstract_zh / applicant.original^2 / applicant.docdb`(`best_fields`)。通用 `patent_name/abstract` 是 `icu_analyzer`(跨语种),分语种 `title_zh/abstract_zh` 是 `ik_smart`(补 CJK 召回),当事人字段补「公司/机构名」召回(如「华为 折叠屏」可按申请人命中),故**中英文查询都能命中**。时效映射 `range(application_date >= 计算起点)`,`_source` 裁剪字段,`highlight` 摘要片段当 snippet。**只触达只读 `_search` 端点**(落在前置 nginx 只读白名单内),绝不构造写/管理 DSL。
- **语义排序复用现有重排**:`PatentResult` 设计为 `SearchResult` 子类,`text_for_rerank()` 返回「专利名+摘要」,**现有 cross-encoder reranker 零改动**即可对 query↔专利打分;`fast` 时按 ES `_score`(存入 `score`)降序。
- **专利 evidence 元数据**:对外返回 `type=patent`。`citation.publication_number` 只作为引用字段;完整结构化元数据进入 `patent` 子对象(`publication_number/application_number/applicant/inventor/ipc_main/cpc_main/country/status/family_id/application_date/publication_date/patent_type/citation_count`)。申请日/公开日用于 `published_date`;摘要进入 `passage.text`(`snippet_type=patent_abstract`);引用数同时进入 `scores.authority`。专利无原生网页,`url` 用 Google Patents 落地页 `https://patents.google.com/patent/{公开号去横线}`(如 `US-2024030484-A1` → `.../US2024030484A1`)。
- **鉴权与网络**:ES 自身**无鉴权、不区分读写**,只读保证完全靠前置 nginx(只读白名单)+ AWS 安全组**来源 IP 白名单**(详见 `~/adhoc-2026-06-15-read-only-es-nginx-in-se4ai-v2.md`)。本项目所在开发机出口 IP 已在白名单,直连可用;**换部署机器需先给新出口 IP 放行 9243**。TLS 证书覆盖该域名,默认 `verify=True`。凭证缺失(无 `PATENT_ES_URL`)则专利能力**静默关闭**,web/学术零影响。
- ⚠️ **库与字段随版本变化**:`epo_docdb_v2` 相比旧 `patents` **无 `claims`/`grant_*`/`current_holder`**,新增 CPC/优先权/同族/国别/法律状态等。`20260620` 相比 `20260615` 修复了**中国授权(CN-B)当事人大面积缺失**(申请人 80.8%→90.1%、发明人 76.1%→85.8%,新增中文原文名),代价是当事人字段从扁平 string 改为 object(breaking,已适配;见 [patent-es-cn-b-missing-applicant-bug.md](./patent-es-cn-b-missing-applicant-bug.md))。换索引(如 `epo_docdb`/`google_patents`)字段结构不同,需各自 `_mapping` 适配(`PATENT_ES_INDEX` 可配)。

### 缓存 — provider 召回级([cache.py](../src/cache.py))

目的:**避免重复调用搜索源 API**(腾讯/百度/SerpAPI/OpenAlex)。缓存的是每个 provider 的原始召回结果(`provider.search` 的返回),不是整体响应。

- **粒度**:provider 召回级。key = `provider|per_provider_k|recency|query`。**改 `top_k` / 重排参数仍命中**召回缓存(只省搜索源 API,重排仍每次走);provider 自身配置(如 OpenAlex `topic_filter`)进程内不变,故不入 key。
- **后端**:`CacheBackend` 抽象接口 + 进程内 `InMemoryCache`(`OrderedDict` LRU + 按 key TTL,带 `threading.Lock` 线程安全,记录命中率)。**接口化预留 Redis**:将来新增 `RedisCache(CacheBackend)` 并在 `build_cache` 加分支即可,engine 无感。单进程 uvicorn 进程内已足够;重启即清空。
- **时效查询不缓存**:`time_sensitive=true`(「最新/今天/2026」等)完全跳过缓存(不读不写),保证新鲜度。
- **防对象污染(重构过渡)**:F-04 完成前，召回结果仍会被旧重排模型写入 `rerank_score/provider_rank`；`RecallCoordinator` 暂时在缓存存取时深拷贝，随后将由不可变阶段模型移除此补偿逻辑。
- **TTL**:非时效结果默认 `CACHE_TTL=21600`(6h)。
- **效果**:同一非时效查询第二次命中,实测省掉 ~2s 搜索源 API(4700ms → 2666ms;剩余为重排耗时,因召回缓存不省重排)。`/health` 暴露 `cache` 统计(size/hits/misses/hit_rate)。
- **未覆盖**:重排 API(SiliconFlow)调用不省 —— 若要让完全相同的查询连重排也省(命中降到毫秒级),需再叠加一层"整体响应缓存",可与召回缓存共存。

---

## 5. 配置开关总表(默认值 vs 推荐值)

> 全部经 `.env` 环境变量控制，由 [config.py](../src/config.py) 的 `Settings.from_env()` 在应用 lifespan 中一次性读取为不可变快照；导入模块不会读取环境。新配置以 `RANKING_PROFILE` 为权威。

| 开关 | 默认值 | 含义 | 推荐(质量优先) |
|------|--------|------|----------------|
| `RANKING_PROFILE` | **`quality`** | `quality`=文本+领域信号；`semantic`=纯文本；`fast`=无文本模型 | `quality`，低延迟场景用 `fast` |
| `RERANK_BACKEND` | `siliconflow` | `siliconflow`/`bge`/`flashrank`/`none` | `siliconflow`(零 GPU、~2.7s) |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 重排模型 | 同默认 |
| `RERANK_THRESHOLD` | `0.3` | 融合前的文本相关性门槛；`0` 等同关闭 | 同默认 |
| `RERANK_THRESHOLD_MODE` | **`prefer`** | `off`=关闭；`prefer`=达标优先并回填；`strict`=硬过滤 | `prefer`；只在确需空结果时使用 `strict` |
| `RERANK_ENABLED` | 兼容字段 | `false → fast`；`true` 使用非 fast 默认档 | 新调用改用 `RANKING_PROFILE` |
| `REWRITE_ENABLED` | **`false`** | L0 LLM 查询改写 | 视查询分布评测后再定 |
| `FUSION_ENABLED` | 兼容字段 | 非 fast 场景中 `true → quality`、`false → semantic` | 新调用改用 `RANKING_PROFILE` |
| `CHUNK_MAX_CHARS` / `CHUNK_OVERLAP` | `400` / `50` | 分块大小与重叠 | 同默认 |
| `SEARCH_TOP_K` / `SEARCH_PER_PROVIDER_K` | `10` / `10` | 返回条数 / 每源召回数 | 同默认 |
| `SEARCH_PROVIDER_TIMEOUT` | `15` | 单源超时(秒) | 同默认 |
| `OPENALEX_API_URL` | `http://localhost:9001` | 学术数据源 = 本地 Chukonu 检索系统基址(其 ES 灌了 5 万条 OpenAlex);配了即启用学术检索 | 指回全量服务可扩覆盖 |
| `OPENALEX_API_KEY` | 空 | 可选 `X-API-Key`(Chukonu 服务未配 `SE4AI_API_KEYS` 时全部放行,留空即可) | 服务开鉴权时再配 |
| `OPENALEX_ENABLED` | 未设置=`auto` | `false` 强制关；`true` 要求 URL；未设置按 URL 自动启用 | 需要临时停用时显式 `false` |
| `OPENALEX_QUERY_REWRITE` | **`true`** | 学术 query 中→英改写(NL→检索词,缓解中文召回弱) | 同默认 |
| `OPENALEX_ACADEMIC_DETECT` | **`true`** | L0 学术意图自动识别开关(关掉则仅 `include_academic=true` 触发) | 同默认 |
| `OPENALEX_PER_PAGE` | `25` | 学术单次召回数(≤100) | 同默认 |
| `OPENALEX_TOPIC_FILTER` / `OPENALEX_MAILTO` | — | 旧公网 API 残留项,当前 Chukonu 后端未用 | 忽略 |
| `PATENT_ES_URL` | 空 | 专利只读 ES 地址(如 `https://search.houdutech.cn:9243`);缺失则专利能力关闭 | 配上即启用专利检索 |
| `PATENT_ES_INDEX` | `epo_docdb_read` | 检索的 ES 索引/别名(读别名,当前指向 `epo_docdb_v2_20260620`,多语种、当事人 object 结构;固定版本号或 `epo_docdb`/`google_patents` 字段结构不同) | 用别名,蓝绿切换免改 |
| `PATENT_ES_ENABLED` | 未设置=`auto` | `false` 强制关；`true` 要求 URL；未设置按 URL 自动启用 | 需要临时停用时显式 `false` |
| `PATENT_ES_VERIFY_TLS` | **`true`** | 校验 TLS 证书(houdutech 证书已覆盖域名) | 同默认 |
| `PATENT_ES_PER_PAGE` | `25` | 专利单次召回数(≤100) | 同默认 |
| `PATENT_DETECT` | **`true`** | L0 专利意图自动识别开关(关掉则仅 `include_patent=true` 触发) | 同默认 |
| `CACHE_ENABLED` | **`true`** | provider 召回级缓存(避免重复调搜索源 API);时效查询不缓存 | 同默认 |
| `CACHE_BACKEND` | `memory` | 进程内 LRU+TTL(预留 `redis`,未实现时回退 memory) | 多实例/持久化再换 redis |
| `CACHE_TTL` | `21600` | 非时效结果缓存 TTL(秒,默认 6h) | 同默认 |
| `CACHE_MAX_SIZE` | `512` | 进程内缓存条目上限(LRU 淘汰) | 同默认 |
| `EXECUTOR_MAX_WORKERS` | `16` | 召回、排序与 PDF 共用的有界线程池 | 按并发与外部限流调整 |
| `MCP_ENABLED` | `auto` | `false`=仅 REST；`true`=MCP 依赖失败则启动失败；`auto`=缺依赖时降级 | 部署已安装 MCP 时保留 `auto` |
| `API_AUTH_TOKEN` | 空 | API 鉴权 token(可逗号分隔多个);配了即对 `/search` 与 `/mcp` 强制 Bearer/X-API-Key | 对外暴露时**必配** |

凭证(任一组齐全即自动启用对应源):`TENCENT_SECRET_ID`+`TENCENT_SECRET_KEY` · `QIANFAN_API_KEY` · `SERPAPI_API_KEY`;`SILICONFLOW_API_KEY`(重排/改写共用);`OPENALEX_API_URL`(学术检索数据源 = 本地 Chukonu 服务,有默认值;独立于 web 源);`PATENT_ES_URL`(专利检索,独立于 web 源,缺失则专利能力静默关闭)。

---

## 6. 评测结论(IR,30 查询,Claude LLM-as-judge,k=10)

> 完整方法论(指标公式、pooling、判分缓存)见 [eval-methodology.md](./eval-methodology.md);最新数据见 [eval/report.md](../eval/report.md)。

| 配置 | NDCG@10 | Recall@10 | P@10 | MRR | 重排延迟 |
|------|---------|-----------|------|-----|---------|
| 腾讯单源 | 0.844 | 0.381 | 0.880 | 0.983 | 0 |
| 百度单源 | 0.753 | 0.399 | 0.900 | 0.936 | 0 |
| SerpAPI 单源 | 0.482 | 0.241 | 0.633 | 0.704 | 0 |
| 三源 + RRF | 0.774 | 0.394 | 0.880 | 0.983 | 0 |
| **三源 + SF 重排** | **0.906** | **0.442** | **0.963** | **0.983** | ~2.7s |
| 三源 + SF + 信号融合 | 0.896 | 0.442 | 0.963 | 0.983 | ~2.7s |

单源检索延迟:腾讯 ~979ms、百度 ~1267ms、SerpAPI ~3426ms。

三条由评测直接支撑的决策:

1. **文本重排是核心质量杠杆** —— 比纯 RRF **+0.13 NDCG**(0.906 vs 0.774),API 化后零 GPU、~2.7s/查询。
2. **旧通用 `FusionReranker` 在该评测集上是负优化** —— 0.896 < 0.906；这组历史实验不是当前按领域设计的 `quality` Profile。三个新 Profile 必须用版本化 E2E 缓存重新 A/B，不能复用旧融合结论或缓存。
3. **SerpAPI 单源弱但聚合有益** —— 单源仅 0.482,但并入后整体 Recall 仍升,补了英文/全球覆盖。

---

## 7. 运行方式

> 引擎运行在 **Python 3.11** venv(`.venv311`,用 `uv` 建;进程内 MCP server 需 ≥3.10)。旧 `.venv`(3.9)保留作回退,此时 `src.api` 自动降级为「仅 REST」(无 MCP)。`scripts/serve.sh` 优先用 `.venv311`。

```bash
cd /home/ec2-user/tavily

# CLI 单次查询
.venv311/bin/python -m src.engine "你的问题"

# 启动服务(REST + 网页端 + MCP);推荐用脚本(自动选 .venv311、可后台)
scripts/serve.sh                 # 前台
scripts/serve.sh -d              # 后台(nohup,日志 /tmp/se.log)
# 等价于:.venv311/bin/uvicorn src.api:app --host 0.0.0.0 --port 8000

# API 调用
curl -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"...","top_k":5}'

# 默认即质量优先；也可显式指定
RANKING_PROFILE=quality scripts/serve.sh

# 低延迟 fast 路径
RANKING_PROFILE=fast scripts/serve.sh
```

### 启用学术检索(OpenAlex 数据,经 Chukonu 服务)

数据源 = 本地 **Chukonu 检索系统**(`http://localhost:9001`,ES 已灌 5 万条 OpenAlex)。配了 `OPENALEX_API_URL`(默认即指向它)即启用,无需公网 key。

```bash
# 0) 确保 Chukonu 服务在跑(默认 :9001;健康检查免鉴权)
curl -s http://localhost:9001/health     # {"status":"healthy"}

# 1)(可选)自定义服务地址 / X-API-Key(服务未配 SE4AI_API_KEYS 时可全留空)
#    echo 'OPENALEX_API_URL=http://localhost:9001' >> .env

# 2) 验证已启用(health 的 academic 应为 true)
curl -s localhost:8000/health        # {"academic": true, ...}

# 3) CLI:学术查询自动触发,打印混排 evidence
.venv311/bin/python -m src.engine "latest survey on diffusion models"

# 4) API:include_academic 显式控制(None=自动 / true=强制开 / false=强制关)
curl -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"graph neural networks survey","include_academic":true,"top_k":5}'
# 返回体含 evidence:[{type:"academic",title,url,passage,citation,access,scores,diagnostics,...}]
```

- 学术意图由 L0 正则自动识别(论文/综述/arxiv/survey/citation 等);非学术查询通常不会产生 `type=academic` evidence。
- ⚠️ OpenAlex 以英文文献为主,**英文学术查询效果显著优于中文**(见 §4 L1′)。

### 启用专利检索(houdutech 只读 ES)

专利能力默认随 `PATENT_ES_URL` 自动启用。本项目所在开发机出口 IP 已在该 ES 的安全组白名单内,直连即可用。

```bash
# 1) .env 配只读 ES 地址
echo 'PATENT_ES_URL=https://search.houdutech.cn:9243' >> .env

# 2) 验证已启用(health 的 patent 应为 true)
curl -s localhost:8000/health        # {"patent": true, ...}

# 3) CLI:专利查询自动触发,打印混排 evidence
.venv/bin/python -m src.engine "钠离子电池正极材料专利"

# 4) API:include_patent 显式控制(None=自动 / true=强制开 / false=强制关)
curl -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"sodium ion battery cathode patent","include_patent":true,"top_k":5}'
# 返回体含 evidence:[{type:"patent",title,url,passage,citation,scores,diagnostics,...}]
```

- 专利意图由 L0 正则自动识别(专利/发明专利/实用新型/公开号/patent/IPC 等);非专利查询通常不会产生 `type=patent` evidence。
- 默认库 `epo_docdb_v2_20260615` 是**全球多语种专利库(英文为主)**,**中英文查询都能召回**(通用 icu 字段 + 中文 ik_smart 字段同时打分)。
- ⚠️ **换部署机器**需先用有 EC2 权限的机器给新出口 IP 放行该 ES 的 9243 端口(安全组操作见 `~/adhoc-2026-06-15-read-only-es-nginx-in-se4ai-v2.md`)。

### MCP 接入(进程内,FastAPI 同进程挂载)

把搜索引擎包成 **MCP server**,与 REST 服务**同一进程、同一端口**:Streamable HTTP 端点 `http://<host>:8000/mcp`,暴露单个工具 `search`。Agent / Claude Code / Desktop 可直接调用,复用同一 `SearchEngine` 单例(零额外进程、零额外端口)。

- **工具 `search(query, top_k=10, include_academic=None, include_patent=None, rerank=None, include_pdf_text=False, ...)`**:返回 LLM-ready 结构化 JSON —— `evidence[]` 按相关性混排,每条带 `type/source/title/url/passage/citation/patent/scores/access/diagnostics`;`patent` 仅在专利证据中承载专属元数据;`meta` 带 `providers_used/reranker/elapsed_ms/counts`。代码:[mcp_server.py](../src/mcp_server.py)(`build_mcp`),挂载在 [api.py](../src/api.py)。
- **工具 `get_pdf_text(work_id, cursor=None, max_chars=8000)`**:续读已抽取的 OpenAlex PDF 正文。Agent 从 `search` 返回的 academic evidence 中读取 `citation.work_id` 与 `access.next_cursor`,调用该工具获取后续 `text/page_from/page_to/next_cursor`。该工具只读缓存,不触发下载解析;REST 同步暴露 `GET /academic/pdf/text/{work_id}`。
- **可答性与部分失败**:`partial_failure=true` 表示至少一个 provider/rerank/PDF 富化子任务失败,但已有 evidence 仍可使用;失败明细在 `failures[]`。`answerability.gaps[]` 明确告诉 Agent 缺少哪类证据(如 `NO_ACADEMIC_EVIDENCE` / `NO_PATENT_EVIDENCE` / `PDF_TEXT_UNAVAILABLE` / `PARTIAL_FAILURE`),`status=not_answerable` 时不应直接输出确定性答案。
- **实现要点**:`FastMCP(stateless_http=True, json_response=True, streamable_http_path="/mcp")`。API 创建时只注册固定 root proxy，lifespan 中由 Container 创建 MCP 后再转发；父 lifespan 显式运行且只运行一次 session manager。显式 REST 路由优先匹配，禁用 MCP 时 `/mcp` 返回 404。引擎 `search()` 是同步阻塞，工具内用 `anyio.to_thread` 卸到线程池。
- **接入消费端**:Claude Code `claude mcp add chukonu-web-search --transport http http://localhost:8000/mcp --header "Authorization: Bearer <token>"`(未开鉴权时去掉 `--header`);或 MCP Inspector 连 `http://localhost:8000/mcp`(在 Inspector 里填 Authorization 头)调试。MCP 服务名(serverInfo.name)为 `chukonu-web-search`。
- 🔐 **鉴权**:`/mcp` 与 `/search` 共用 `API_AUTH_TOKEN`(见下「鉴权」小节);开启后 MCP 客户端需带 `Authorization: Bearer <token>`,否则握手 401。

```bash
# 验证 MCP(需 .venv311):initialize → tools/list → 调一次 search
# 开了鉴权就带 token;未开鉴权时把 headers 设为 None
.venv311/bin/python - <<'PY'
import os, anyio, json
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
tok = next((l.split('=',1)[1].strip() for l in open('.env') if l.startswith('API_AUTH_TOKEN=')), '')
headers = {"Authorization": f"Bearer {tok}"} if tok else None
async def main():
    async with streamablehttp_client("http://localhost:8000/mcp", headers=headers) as (r,w,_):
        async with ClientSession(r,w) as s:
            await s.initialize()
            print([t.name for t in (await s.list_tools()).tools])
            res = await s.call_tool("search", {"query":"什么是 RAG","top_k":3})
            print(json.loads(res.content[0].text)["meta"]["counts"])
anyio.run(main)
PY
```

- **网页端**:`GET /` 返回单文件搜索界面([static/index.html](../src/static/index.html)),无构建依赖。
- **召回缓存**:默认开(`CACHE_ENABLED=true`)。同一非时效查询第二次命中可省搜索源 API;`GET /health` 的 `cache` 字段看命中率。设 `CACHE_ENABLED=false` 关闭。
- **从本地访问**:EC2 无公网 IP,用 SSH 隧道 `ssh -i <key.pem> -N -L 8000:localhost:8000 ec2-user@<EC2>`,浏览器开 `http://localhost:8000/`;MCP 也走同一隧道(`http://localhost:8000/mcp`)。

### 鉴权(API token)

在 `.env` 配 `API_AUTH_TOKEN`(可逗号分隔多个)即对**数据出口** `/search` 与 `/mcp` 强制校验;留空=不鉴权(本地开发默认)。中间件统一覆盖 REST 与挂载的 MCP 子应用([api.py](../src/api.py) `auth_middleware`,`hmac.compare_digest` 常量时间比较)。

- **公开放行**:`/`(网页壳)、`/health`、`/docs`/`/openapi.json`/`/redoc` —— 让页面能加载、健康检查可探活、文档可读;真正的数据出口受保护。
- **凭证位置**:`Authorization: Bearer <token>` 或 `X-API-Key: <token>`(二选一)。网页端在「高级选项 → API Token」填同一 token,存浏览器 localStorage,`/search` 请求自动带上;401 时提示填 token。
- **生成**:`python -c "import secrets;print(secrets.token_urlsafe(32))"`。`.env` 已 gitignore,勿提交。
- `GET /health` 的 `auth`/`mcp` 字段反映是否开启鉴权 / 是否挂载 MCP。
- ⚠️ 仍不要把端口裸绑公网;鉴权是底线,生产建议再叠 TLS / 反代 / 速率限制。

```bash
TOKEN=$(grep ^API_AUTH_TOKEN= .env | cut -d= -f2)
curl -s -X POST localhost:8000/search -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"query":"...","top_k":3}'
```
- **依赖**:`.venv311`(Python 3.11,`uv` 建)= `requirements.txt` + `mcp`;**未装** torch/sentence-transformers(本地 `RERANK_BACKEND=bge` 才需,默认 siliconflow API 不需要)。磁盘在 `/`(500G,充足)。

---

## 8. 现状与待办

**已落地(相对调研文档的新增)**:L0 LLM 查询改写、SerpAPI 第三源，以及统一的 `quality/semantic/fast` 排序 Profile 和 `off/prefer/strict` 阈值策略。旧 `FusionReranker` 仅保留历史评测兼容。**OpenAlex 学术检索**(L0 学术意图识别 + 内部独立重排 + 对外 `type=academic` evidence)已实现并通过端到端验证。**专利检索(houdutech 只读 ES)**(L0 专利意图识别 + 内部独立重排 + 对外 `type=patent` evidence + multi_match 中文检索)作为第二条垂直支线已实现并通过端到端验证。**provider 召回级缓存**(进程内 LRU+TTL,接口化预留 Redis;时效查询不缓存)已落地,避免重复调用搜索源 API。

**与调研的关键差异**:

| 调研建议 | 实际落地 | 理由 |
|---------|---------|------|
| Brave 作主搜索源 | 腾讯 + 百度 + SerpAPI | Brave 取消免费层且中文弱;腾讯/百度免新成本、中文强、自带正文 |
| Trafilatura/Crawl4AI 抓正文(L2) | 省略 | 腾讯/百度接口直接返回正文 |
| 自托管 BGE 重排 | SiliconFlow API(同模型权重) | 质量持平、零 GPU、延迟降一个数量级 |
| 学术检索自建向量库 | OpenAlex API 召回 + 复用现有 cross-encoder 重排 | MVP 零新增基础设施;先接 API 补垂直能力,向量索引列为演进 |
| 专利检索自建索引 | 接现成只读专利 ES + 复用现有 cross-encoder 重排 | 既有集群数据现成;镜像学术支线范式,零新增基础设施 |
| Redis 结果缓存 | 进程内 LRU+TTL,接口化预留 Redis | 单进程 uvicorn 够用、零依赖零部署;多实例/持久化再换 Redis |
| firewall/MCP | MCP 已做(进程内挂载)/ firewall 未做 | 引擎升 3.11 后 FastAPI 同进程挂 MCP server,零额外进程;安全 firewall 仍待做 |

**待办**(优先级见调研文档 §0.6):

- [ ] 🟡 近重复去重(SimHash/MinHash 或 MMR)—— 当前只去精确 URL,转载/聚合页仍占位。
- [ ] 🟡 L5 答案合成(`include_answer`,LLM 带引用)。
- [ ] 🟡 学术检索增强:中文学术 query 中→英改写、补 PubMed/arXiv 源、扩评测集量化学术查询质量。
- [ ] 🟡 专利检索增强:① `bool` 结构化过滤(IPC/CPC 分类 / 申请人 / 国别 / 日期区间 / 法律状态);② 多索引适配(`epo_docdb`/`google_patents` 字段结构不同,需各自 mapping);③ 中文召回优化(专门查 `title_zh/abstract_zh` 分支);④ 扩评测集加 `patent` 类查询量化质量、复测 rerank 阈值是否误杀;⑤ 利用同族 `family_id` 去同族重复、`cited_doc_ids` 做引用信号。
- [ ] 🟡 缓存增强:整体响应缓存(连重排也省,命中降到毫秒级)、Redis 后端(多实例共享/持久化)。
- [ ] 🟡 横切能力:**MCP 接入已落地**(进程内 Streamable HTTP `/mcp`,工具 `search`)+ **API token 鉴权已落地**(`/search` 与 `/mcp` 共用 `API_AUTH_TOKEN`);仍待:安全 firewall(提示注入/PII)、生产级 TLS/反代/速率限制、多租户/OAuth。
- [ ] 🟢 扩评测集到 50–100 条 + 端到端 RAG 评测(faithfulness / context precision)。
- [ ] 🟢 重排提速(ONNX+int8 / fp16)、分数缓存、超时降级。
- [ ] 🟢 垂直支线编排抽象:学术 + 专利已是两条平行支线,后续支线变多时把「垂直源」抽象成统一编排(`List[VerticalSource]`),替代当前镜像式 codepath。

---

## 关联文档

| 文档 | 职责 |
|------|------|
| [本篇] tech-route-summary.md | **当前实现快照** —— 分层/选型/开关/评测结论/运行 |
| [agent-search-engine-tech-research.md](./agent-search-engine-tech-research.md) | **调研与选型对比** —— 各方案权衡、为什么这么选、风险 |
| [academic-search-engine-feasibility.md](./academic-search-engine-feasibility.md) | **学术引擎可行性** —— 数据源全景、自建可行性、OpenAlex 接入定位 |
| [eval-methodology.md](./eval-methodology.md) | **评测体系** —— 指标公式、LLM-as-judge、pooling、复现 |
| [mcp-usage.md](./mcp-usage.md) | **MCP 使用文档** —— `chukonu-web-search` 工具说明、鉴权、接入(Claude Code/Desktop/Inspector)、本地/外网 |
