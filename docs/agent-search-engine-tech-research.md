# 自建 Agent 搜索引擎技术路线调研

> 目标:从头搭建一个类似 [Tavily](https://www.tavily.com/) 的、面向 AI Agent / LLM 的通用 Web 搜索引擎。
> 路线:**元搜索聚合层**(meta-search aggregation),即包装现有搜索源 + 抓取正文 + LLM 重排/摘要,而非自建全网爬虫与倒排索引。
> 编写日期:2026-06-03 ｜ 更新:2026-06-04(补「实现现状」+ 落地评测结果)
> 侧重:架构与组件调研 + 技术选型对比。**第 0 节为已落地实现,第 1–6 节为原始调研(仍作选型参考)。**

---

## 1. 核心洞察:Tavily 不是搜索引擎,是「Agent 的 Web 访问层」

调研的第一个关键结论:**Tavily 本身并不自建全网索引**。它本质是一个为机器(LLM)而非人类设计的「检索后处理管线」,把多个现有 Web 源的结果聚合、抓正文、清洗、重排、摘要,最后以结构化 JSON 返回。

传统搜索引擎(SERP)返回一长串标题/链接/图片,是给**人**导航用的;Agent 需要的是**正文内容本身**的结构化数据,直接喂给 LLM。Tavily 的全部设计都围绕一个原则:**输出为 LLM 而非人类构建**。这带来两个核心价值:

1. **Token 效率 / 内容清洗** —— 过滤掉 HTML/CSS/导航/广告等噪声,只留正文,避免撑爆上下文窗口。
2. **降低幻觉** —— 提供干净、有事实依据、带来源链接的上下文。

> 这条路线对我们的意义:**MVP 工程量最小、最快上线**。我们不需要去解决爬全网、存 PB 级数据、反爬、排序质量这些「真搜索引擎」级别的难题,而是把精力放在**聚合 + 正文提取 + 重排 + 摘要**这条后处理管线上。

Tavily 对外暴露的可组合 API:`Search`(发现页面)、`Extract`(抓正文)、`Map`/`Crawl`(站点结构/爬取)、`Research`(多轮深度研究)。设计哲学是**可组合积木**——价值来自如何把它们编排成工作流。

---

## 0. 实现现状(已落地 MVP)

> 本节记录**实际已构建并验证可用**的 MVP。与下方原始调研的差异均为环境/资源约束下的务实选择,理由见文末「与调研的差异」。

### 0.1 已落地的数据流

```
POST /search {query, top_k, ranking_profile?, rerank_threshold_mode?}
      │
      ▼
 L0 查询理解(规则版)              ── 规范化(NFKC/压空白)+ 时效识别 + 输入校验
      │  产出 SearchPlan{normalized_query, recency, time_sensitive}
      ▼
 多源并发检索(ThreadPool)         ── 腾讯 SearchPro + 百度千帆 AI 搜索
      │  时效下传(腾讯 FromTime/ToTime · 百度 search_recency_filter)
      │  两源均直接返回正文摘要 → L2 抓取层省略
      ▼
 跨源处理
   ├─ quality(默认):文本相关性 + 领域信号
   ├─ semantic:仅文本相关性
   └─ fast:不调用文本模型;Web RRF,垂直源按来源原始分
      阈值:off=关闭 / prefer=达标优先并回填 / strict=硬过滤
      │
      ▼
 归一化 JSON  {query, results:[{url,title,snippet,content,date,site,source,rerank_score}], ...}
```

### 0.2 代码结构

```
src/
  config.py              # .env 加载;凭证→自动启用对应源;重排 backend/model/device
  l0.py                  # L0 查询理解(规则版):规范化 + 时效识别 + 输入校验 → SearchPlan
  models.py              # SearchResult / SearchResponse / SearchPlan(pydantic)
  providers/
    base.py              # SearchProvider 抽象基类
    tencent.py           # 腾讯 SearchPro(TC3-HMAC-SHA256 签名,纯标准库)✅
    baidu.py             # 百度千帆 AI 搜索(Bearer + 72 字符查询裁剪)✅
  pipeline/
    chunk.py               # L3 文档分块:段落→句子→字符三级拆分,合并短段落+重叠
    dedup.py               # URL 归一化跨源去重
    fusion.py              # RRF 多源融合(provider_rank)
    ranking_options.py     # quality/semantic/fast 与 off/prefer/strict 的规范配置和旧字段兼容
    rerank.py              # 段落级文本打分 + 领域排序策略 + NoOp 兜底
  engine.py              # 编排:多源并发→去重/融合→重排→TopK
  api.py                 # FastAPI:GET /(网页) · POST /search · GET /health
  static/index.html      # 网页搜索界面(单文件,无构建依赖)
eval/                    # IR 评测体系(见 0.4)
requirements.txt         # fastapi/uvicorn/pydantic/requests/flashrank/sentence-transformers/anthropic
.venv/                   # 装在 /data(根分区仅 2.8G,torch 等勿装根)
```

### 0.3 已落地技术栈

| 层 | 实际选型 | 说明 |
|----|---------|------|
| API | **FastAPI REST**(`/search`, `/health`)+ **网页端**(`/`) | MCP 暂未做 |
| 查询理解 L0 | **规则版**(规范化 + 时效识别 + 输入校验)✅ | 时效接入两源原生过滤;LLM 改写/子查询拆分待后续 |
| 搜索源 L1 | **腾讯 SearchPro + 百度千帆 AI 搜索** | 均已验证;中文覆盖好、自带正文 |
| 抓取 L2 | **省略** | 两源直接返回正文摘要 |
| 去重 L3 | URL 归一化去重 / RRF 融合 / **文档分块(chunk)** ✅ | chunk 供段落级重排使用 |
| 重排 L4 | `quality/semantic/fast`;SiliconFlow API (BGE-v2-m3) / 本地 BGE / FlashRank | 段落级打分 + 按领域信号;阈值支持 `off/prefer/strict` |
| 合成 L5 | 未做 | 留 hook |
| 缓存/安全/可观测 | 未做 | 后续 |

**运行**:
```bash
cd /data/tavily
.venv/bin/python -m src.engine "你的问题"                       # CLI
.venv/bin/uvicorn src.api:app --host 0.0.0.0 --port 8000        # 服务(REST + 网页)
curl -X POST localhost:8000/search -d '{"query":"...","top_k":5}' -H 'Content-Type: application/json'
```

**网页端**:服务启动后,`GET /` 返回一个单文件搜索界面([src/static/index.html](../src/static/index.html))——搜索框 + 结果卡片(标题/重排分/正文/来源标签)、改写与时效标识、耗时元信息。

**从本地访问网页(SSH 隧道)**:EC2 通常无公网 IP,用 SSH 端口转发即可,无需动安全组、不暴露公网。

```bash
# 1) 服务端启动(保持运行;或 nohup 后台)
cd /data/tavily && .venv/bin/uvicorn src.api:app --host 0.0.0.0 --port 8000

# 2) 本地电脑开隧道:本地 8000 → EC2 的 8000
ssh -i <密钥.pem> -N -L 8000:localhost:8000 ec2-user@<EC2地址>
#   -L 本地端口:目标:目标端口   -N 只转发不开 shell

# 3) 本地浏览器打开(页面同源调用 /search,一并走通)
#    http://localhost:8000/
```
关掉隧道(Ctrl+C)即断开。同 VPC 内的其它机器可直接访问内网 `http://<内网IP>:8000/`(需安全组放行 8000)。
> ⚠️ 当前 `/search` 无鉴权,**不要**直接绑 Elastic IP 裸露公网;需对外请先加 token 鉴权。

### 0.4 评测结果(IR,30 中文/英文查询,Claude LLM-as-judge,k=10)

> 评测集已扩到 30 条(8 类:事实/时效/多跳/长尾/混合/how-to/口语化/英文/领域)。下表为三源 + SiliconFlow API 重排的最新对照。

| 配置 | NDCG@10 | Recall@10 | P@10 | MRR | 重排延迟 |
|------|---------|-----------|------|-----|---------|
| 腾讯单源 | 0.844 | 0.381 | 0.880 | 0.983 | 0 |
| 百度单源 | 0.753 | 0.399 | 0.900 | 0.936 | 0 |
| SerpAPI 单源 | 0.482 | 0.241 | 0.633 | 0.704 | 0 |
| 三源 + RRF | 0.774 | 0.394 | 0.880 | 0.983 | 0 |
| **三源 + SF 重排(生产默认)** | **0.906** | **0.442** | **0.963** | **0.983** | ~2.7s |
| 三源 + SF + 信号融合 | 0.896 | 0.442 | 0.963 | 0.983 | ~2.7s |

**结论**:① SiliconFlow API(BGE-v2-m3)重排相比纯 RRF **+0.13 NDCG**,是核心质量杠杆,且 API 化后 ~2.7s/查询、零 GPU;② **信号融合对通用查询是负优化**(0.896 < 0.906)—— 新鲜度信号误伤了事实类查询的权威老页面(百科等),已默认关闭,仅时效场景手动开;③ SerpAPI 单源弱(0.482)但三源聚合后 Recall 仍升,补充了英文/全球覆盖。评测体系见 [eval/](../eval/),可一条命令���现对照。

> 历史(12 条评测集):双源 + BGE 本地 NDCG 0.948。注:30 条评测集更难更多样,数值不可与 12 条直接比。

### 0.4.1 关键评测教训

**信号融合的负优化是扩评测集才发现的** —— 12 条时人工抽检看着"时效查询新闻排前面了"很合理,但 30 条全量 A/B 才暴露它整体拉低 NDCG。印证文档反复强调的:**每项改动都要在评测集上量化,不凭感觉**。

### 0.5 与调研的差异(及理由)

| 调研建议 | 实际落地 | 理由 |
|---------|---------|------|
| Brave 作主搜索源 | 腾讯 + 百度 | Brave 免费层已取消(需绑卡)且中文/境内访问弱;腾讯+百度免新成本、中文强、已验证 |
| Trafilatura/Crawl4AI 抓正文(L2) | 省略 | 腾讯/百度接口直接返回正文摘要,L2 多余 |
| 重排起步可选 | SiliconFlow API(BGE-v2-m3)为默认,免费零 GPU 依赖;可切回本地 BGE | 评测证明质量持平( NDCG 0.946 vs 0.944),延迟降 19 倍 |
| Redis/firewall/MCP | 未做 | MVP 聚焦检索闭环,列为后续 |

### 0.6 L3 / L4 优化 TODO

> 按优先级排列;每项落地后都用现有 IR 评测体系([eval/](../eval/))量化 NDCG/P@k 变化,**不凭感觉调**。

**L3(清洗 / 分块 / 去重)**

当前:跨源 URL 归一化去重 + 文档分块([src/pipeline/chunk.py](../src/pipeline/chunk.py))。

- [x] 🔴 **文档分块(chunk)** ✅ —— 段落→句子→字符三级拆分,合并短段落+重叠。chunk_max_chars=400,overlap=50 可配。
- [ ] 🟡 **近重复去重** —— 当前只去精确 URL;转载/聚合页(不同 URL、内容几乎相同)仍占多个结果位。用内容指纹(SimHash/MinHash)或 MMR 去近重复,提升多样性。

**L4(重排)**

当前:段落级重排([src/pipeline/rerank.py](../src/pipeline/rerank.py))——逐 chunk 打分→max-pooling→sigmoid 归一化，并通过 `quality/semantic/fast` Profile 与 `off/prefer/strict` 阈值策略控制生产行为。

- [x] 🔴 **段落级重排,替代「前 2000 字符硬截断」** ✅ —— 文档切 chunk 后逐块与 query 组 pair 交给 cross-encoder 打分,每文档取 chunk 最高分(max-pooling)。彻底解决 BGE 512 token 上限 vs 2000 字符截断的问题。
- [x] 🔴 **相关性阈值策略** ✅ —— 阈值作用于融合前文本分:`off` 关闭，默认 `prefer` 把达标项置前并用低分项回填，`strict` 才硬过滤；NoOp scorer 自动跳过。
- [x] 🟡 **按领域辅助信号融合** ✅ —— `quality` 融合 Web RRF、论文引用/新鲜度/venue/OA 与专利来源分/新鲜度/引用/状态；`semantic` 只看文本，`fast` 不调用文本模型。旧通用 `FusionReranker` 的负优化结论不直接等同于新 `quality` Profile，需重新 A/B。
- [ ] 🟡 **MMR 多样性** —— rerank 后去近重复、提升结果多样性(与 L3 近重复去重呼应)。
- [ ] 🟡 **混合召回再重排** —— 候选量大时先 BM25/dense + RRF 粗筛 top-N 再交 cross-encoder(当前候选 ~20,暂不急)。
- [ ] 🟢 **GPU 推理优化** —— fp16/bf16 + 调 batch_size(~2x);BGE-v2-m3 支持指定推理层数(layerwise)加速。
- [ ] 🟢 **ONNX + int8 量化** —— CPU/GPU 均提速数倍(也能缓解开发机 CPU 上 35s/查询的问题)。
- [ ] 🟢 **模型档位可切** —— `bge-reranker-base`(278M 快)↔ `v2-m3`(568M 准)↔ `v2-gemma`(更强更大),已有 `RERANK_MODEL` 配置支持。
- [ ] 🟢 **rerank 分数缓存** —— `(query, url) → score` 短 TTL 缓存,重复查询省算力。
- [ ] 🟢 **超时降级** —— rerank 超时回退 RRF 顺序,保证可用性。

> ✅ **L3 分块 → L4 段落级重排 + Profile + 明确阈值策略** 已落地。下一步建议:用版本化 E2E 缓存对三个 Profile 做扩评测集 A/B，并人工抽检 judge 一致性。

---

## 2. 系统架构与数据流

```
                         ┌─────────────────────────────────────────────┐
   用户/Agent 查询 ──────▶│  API 网关 (REST / MCP / SDK)                  │
                         │  鉴权 · 限流 · 参数自动选择(auto-params)      │
                         └───────────────────────┬─────────────────────┘
                                                 │
                  ┌──────────────────────────────▼──────────────────────────────┐
                  │  L0  查询理解层                                                │
                  │  查询改写 / 意图识别 / 子查询拆分 / 时效性判断                  │
                  └──────────────────────────────┬──────────────────────────────┘
                                                 │
       ┌─────────────────────────┬───────────────┴───────────────┬─────────────────────────┐
       ▼                         ▼                               ▼                          ▼
 ┌───────────┐           ┌───────────┐                   ┌───────────┐             【横切关注点】
 │ 搜索源 A  │           │ 搜索源 B  │     ...           │ 搜索源 N  │            ┌──────────────┐
 │  (Brave)  │           │ (SerpAPI) │                   │  (Exa)    │            │ 缓存 (Redis) │
 └─────┬─────┘           └─────┬─────┘                   └─────┬─────┘            │ 动态结果缓存 │
       └───────────────────────┴────────────┬────────────────┘                  └──────────────┘
                                             │  L1 结果聚合 + 去重(URL/内容指纹)  ┌──────────────┐
                                             ▼                                    │ 安全 Firewall │
                  ┌──────────────────────────────────────────────────┐          │ 提示注入检测  │
                  │  L2  正文抓取 / 提取                                │          │ PII / 恶意源  │
                  │  并发抓取 → HTML→Markdown → onlyMainContent         │◀────────▶│ 内容校验      │
                  └──────────────────────────┬───────────────────────┘          └──────────────┘
                                             │  L3 清洗 / 分块(chunk)             ┌──────────────┐
                                             ▼                                    │ 可观测性      │
                  ┌──────────────────────────────────────────────────┐          │ 日志/指标/追踪│
                  │  L4  重排序 (Reranker, cross-encoder)              │          └──────────────┘
                  │  按 query-doc 相关性打分 → Top-K                    │
                  └──────────────────────────┬───────────────────────┘
                                             │  L5 (可选) 摘要 / 合成
                                             ▼
                  ┌──────────────────────────────────────────────────┐
                  │  结构化 JSON 输出                                  │
                  │  {results:[{url, title, content, score}], answer} │
                  └──────────────────────────────────────────────────┘
```

整条管线在**单次 API 调用**内完成 raw web → LLM-ready content 的转换。

---

## 3. 核心组件逐层拆解 + 技术选型对比

### L0 — 查询理解层

| 能力 | 做法 | 备注 |
|------|------|------|
| 查询改写 | 用小模型(Haiku/本地小模型)把口语化问题改写成检索友好关键词 | 影响召回质量 |
| 子查询拆分 | 复杂问题拆成多个原子查询并发检索 | Tavily `Research` 端点的核心 |
| 时效性判断 | 识别"最新/今天/2026"等时间意图,决定是否走缓存 | 影响缓存策略 |
| auto-params | 根据意图自动选择搜索深度(fast/basic/advanced) | Tavily 的差异化特性 |

MVP 阶段可先用简单规则 + 一次 LLM 改写,不必上多轮。

---

### L1 — 搜索源聚合(最关键的选型)

> ⚠️ **市场背景**:微软已于 **2025 年 8 月退役 Bing Search API**,这是整个市场洗牌的导火索,也是不要把 Bing 作为基座的原因。

| 方案 | 类型 | 索引/数据来源 | RAG 适配度 | 关键权衡 | 免费额度 |
|------|------|--------------|-----------|---------|---------|
| **腾讯 SearchPro** ⭐(已用) | 托管 API,联网搜索 | 腾讯自有 | 高(直接返回 title/passage/正文/score) | 中文强、自带正文、TC3 签名鉴权(SecretId+Key);英文/全球弱 | 按量计费 |
| **百度千帆 AI 搜索** ⭐(已用) | 托管 API | 百度自有索引 | 高(返回正文摘要,可选大模型总结) | 中文/境内最佳、Bearer 单 key;查询限 72 字符·单轮、英文弱 | ~100 次/天 |
| **Brave Search API** | 托管 API,独立索引 | 自有 300 亿+页面独立索引 | 中(普通端点 SERP;新 LLM Context 端点给 markdown 正文) | 唯一可规模化的西方独立索引、隐私好;⚠️ **2026-02 取消免费层**,改 $5/月赠额(~1000 次)+ 需绑卡 + 署名 | 无(仅 $5 赠额) |
| **SerpAPI** | 抓取层(非自有索引) | 抓 Google/Bing/DuckDuckGo 等 25+ 引擎 | 低(只返回 SERP 元数据,**无正文**,必须配抓取工具) | 多引擎覆盖最广,适合 SEO/竞品监控;对搜索引擎有不可靠依赖 | 100 次/月 |
| **Exa** | 神经/语义搜索 | embedding 语义检索 + 自有爬虫(偏信息密集内容) | 高(为 RAG/Agent 设计) | 按意义而非关键词,能找到关键词找不到的;但可能返回"概念相似却不相关"结果、credit 消耗不可控、需另配抓取 | 1000 credits |
| **SearXNG** | 开源自托管元搜索 | 聚合多家公共引擎 | 中(需自建管线,只给 snippet) | 完全可控、零 API 费、可私有化;运维成本高、高 QPS 上游会封 | 自托管 |
| Google Custom Search | 托管 API | Google | 低 | 额度极小、限制多 | 100 次/天 |

**选型建议**:
- **中文 / 境内场景(本项目已采用)**:**腾讯 SearchPro + 百度千帆** 双源 —— 中文覆盖强、自带正文、免新成本。
- **英文 / 全球通用**:`Brave Search API`(独立索引)或 Serper(Google 结果),百度仅作中文补充。
- **追求零成本/可控**:自托管 `SearXNG` + 抓取层。
- **多源融合**:同时查多源,用 **RRF(Reciprocal Rank Fusion)** 合并(公式见 L4)。
- 注意:**所有厂商对比多为自评,务必用自己的真实查询分布跑评测**(本项目已建 IR 评测体系,见第 0.4 节)。

---

### L2 — 正文抓取 / 提取

搜索源大多只给链接和 snippet,要拿到**正文**必须单独抓取并把脏 HTML 转成干净 Markdown(Markdown 保留标题/列表等语义结构,利于后续 embedding 和 LLM 理解)。两条技术路线:**启发式** vs **ML 驱动**。

| 方案 | 类型 | 部署 | JS 渲染 | 优势 | 劣势 | 许可/成本 |
|------|------|------|---------|------|------|----------|
| **Trafilatura** ⭐ | 启发式库(Python) | 本地 | ❌ | 正文提取 F1 0.958、本地处理隐私好、内置 readability/jusText 多级回退、零成本 | 不渲染 JS、动态站点抓不全 | 开源,本地 |
| **Crawl4AI** ⭐ | 开源库(Playwright) | 本地/自有云 | ✅ | 自带 LLM-aware 分块、完全自有管线、无按页计费 | 需自己运维 headless 浏览器 | 开源 |
| **Firecrawl** | 托管 API / 自托管 | 云 | ✅ | 自动渲染 SPA、`onlyMainContent` 过滤强、可整站爬、产出 Markdown/JSON/截图,生产级 RAG 首选 | 按页计费、核心 AGPL-3.0 | 500 credits 免费;100k credits/$83 |
| **Jina Reader** | 托管 API | 云 | 部分 | `r.jina.ai/<url>` 即用、无需 key、自动图片描述、原生 PDF、Apache-2.0 友好 | 有限流、URL 发往第三方、JS 重的页面输出不稳 | 新 key 1000 万 token,之后 ~$0.02/百万 token |
| Diffbot | 托管 API | 云 | ✅ | 自动分类实体(文章/产品/人物…)、返回结构化 JSON | 偏结构化抽取、成本高 | 商业 |

**选型建议**:
- **本地/隐私优先 + 静态页面为主**:`Trafilatura`(快、免费、质量高),JS 页面回退到 `Crawl4AI`。
- **生产 RAG、要 SPA 渲染又不想自运维**:`Firecrawl`。
- **混合策略(推荐)**:先用 Trafilatura 试抓,正文过短/失败再升级到带渲染的 Crawl4AI / Firecrawl —— 既省钱又覆盖动态站点。

---

### L3 — 清洗与分块

| 步骤 | 做法 |
|------|------|
| 去噪 | 去导航/页脚/广告/脚本(抓取层 `onlyMainContent` 已做大部分) |
| 标准化 | 统一为 Markdown,去重空白、规整标题层级 |
| 分块 chunk | 按语义/标题分块,保留来源 URL 与位置(供引用) |
| 去重 | URL 归一化 + 内容指纹(SimHash/MinHash)跨源去重 |

---

### L4 — 重排序(质量分水岭)

向量/关键词召回快但**丢失细粒度的 query-doc 交互**;**cross-encoder reranker** 把 query 和 doc 一起编码联合打分,质量通常 **+5~+15 NDCG@10**,常常是"能用的 RAG"和"答不对问题的 RAG"之间的差别。

**推荐的生产级检索流水线(混合召回 + 重排)**:
```
1. BM25(词法)        → top 50   (抓精确关键词:产品码、品牌名)
2. Dense bi-encoder   → top 50   (抓语义/改写)
3. RRF 融合           → top 100  score(d)=Σ 1/(k+rank_i(d)), k=60
4. Cross-encoder 重排 → top 10
5. LLM 生成
```

| Reranker | 类型 | 部署 | 语言 | 备注 |
|----------|------|------|------|------|
| **BGE-Reranker-v2-m3** ⭐ | 开源 cross-encoder | 自托管 | 中日韩最强(中文语料充分) | **中文场景首选**、可控、省钱 |
| **Cohere Rerank v3.5** | 专有 API | 云 | 100+ 语言 | 支持 JSON 等半结构化、4096 token、有 Nimble 低延迟版 |
| **Jina Reranker v2** | API/开源 | 云/自托管 | 多语言 | 面向 agentic RAG,有多模态版 rerank-m0 |
| Pinecone Rerank V0 | API | 云 | - | BEIR 基准 NDCG@10 平均最高(12 数据集中 6 个第一),512 token |
| mxbai / Voyage rerank-2 / FlashRank | 开源/API | 混合 | - | 轻量/可选 |
| ColBERT(late-interaction) | 开源 | 自托管 | - | 2026 视为小众;标准 bi+cross 管线更简单且质量相当 |

**选型建议**:面向中文/通用,**自托管 BGE-Reranker-v2-m3** 起步;需要广覆盖多语言或不想运维则用 Cohere Rerank。**务必在自己的真实查询上做评测 + 并发下的延迟基准**。

---

### L5 — 摘要 / 合成(可选)

- **basic 模式**:每个 URL 给一段 NLP 摘要(平衡相关性与延迟)。
- **advanced 模式**:每个 URL 多个语义相关片段,精度最高、延迟更大。
- **answer 模式**:把 Top-K 正文喂给 LLM 直接合成带引用的答案(类似 Tavily `include_answer`)。
- 模型按 3 层路由:简单摘要用 Haiku/本地小模型,复杂合成用 Sonnet/Opus,控成本。

---

### 横切关注点

| 关注点 | 做法 | 对标 Tavily |
|--------|------|-------------|
| **缓存** | 动态结果缓存(Redis)+ agent-native 索引,流量增长时保持低延迟 | Tavily 的「production-grade retrieval stack」核心 |
| **安全 Firewall** ⭐ | 在 Agent 与公网之间做防火墙:检测**提示注入**、拦 PII 泄露、过滤恶意源,内容入模前校验 | Tavily 的核心卖点,可复用本仓库 `aidefence_scan` 类工具 |
| **API 层** | 同时提供 REST + SDK + **MCP Server**,兼容 LangChain / AutoGen / Vercel AI SDK | 降低 Agent 接入成本 |
| **可观测性** | 每层延迟、命中率、来源分布、成本埋点 | 排障与成本优化 |

> ⚠️ 一个值得注意的取舍:Tavily 刻意**只做文本**,不支持视频/音频/图片检索,以换取速度和与语言模型的契合度。MVP 建议同样先聚焦文本。

---

## 4. 推荐 MVP 技术栈(元搜索聚合路线)

> 下表为**原始调研推荐**;**实际落地**见第 0.3 节(搜索源换为腾讯+百度、L2 省略、重排用 BGE)。

| 层 | MVP 选型 | 升级路径 |
|----|---------|---------|
| API/网关 | FastAPI(REST)+ MCP Server | 加限流(Redis)、鉴权、auto-params |
| 查询理解 | 单次 LLM 改写(Haiku) | 子查询拆分、多轮 Research |
| 搜索源 | **Brave Search API**(+ 可选 Exa) | 多源 + RRF 融合 / 自托管 SearXNG |
| 正文抓取 | **Trafilatura**,失败回退 **Crawl4AI** | 引入 Firecrawl 处理重 JS 站 |
| 去重/分块 | URL 归一化 + SimHash + 语义分块 | - |
| 重排序 | **BGE-Reranker-v2-m3**(自托管) | Cohere / Jina 多语言 |
| 合成 | 可选 `include_answer`,LLM 合成带引用 | 3 层模型路由 |
| 缓存 | Redis 结果缓存 | agent-native 索引 |
| 安全 | 提示注入/PII 检测(`aidefence_*`) | 完整 firewall |
| 可观测 | 结构化日志 + 延迟/成本指标 | 全链路追踪 |

**技术栈一句话**:`FastAPI + Brave API + Trafilatura/Crawl4AI + BGE Reranker + Redis + (可选)LLM 合成`,对外用 REST + MCP 暴露。

---

## 5. 关键风险与权衡

| 风险 | 说明 | 缓解 |
|------|------|------|
| **对外部搜索源的依赖** | Bing API 退役已证明"包装别人引擎"是技术栈关键点的脆弱依赖 | 多源冗余 + 抽象适配层,源可热插拔;长期可自建垂直索引(混合路线) |
| **抓取被封 / 反爬** | 高频抓取会被限流或封 IP | 速率控制、代理池、缓存、尊重 robots.txt |
| **成本不可控** | Exa credit、Firecrawl 按页、LLM token 叠加 | 缓存命中优先、Trafilatura 兜底、3 层模型路由 |
| **结果质量** | 召回/重排不好直接体现为答错 | 混合召回 + cross-encoder 重排,用真实查询持续评测 |
| **提示注入/合规** | 公网内容含恶意指令、PII | Firewall 层入模前校验(对标 Tavily 卖点) |
| **延迟** | 多源 + 抓取 + 重排串起来易超时 | 并发抓取、缓存、可配置 depth、超时降级 |

---

## 6. 下一步建议

- [x] **跑通最小闭环**:搜索源 → 重排 → 返回 JSON(已用腾讯+百度,源自带正文省去抓取)。
- [x] **建评测集 + IR 评测**:12 条真实查询 + LLM-judge,跑通"加重排前后 / 不同源 / RRF vs BGE"对照(见第 0.4 节)。
- [ ] **扩评测集**到 30–50 条 + 人工抽检 judge 一致性,让结论更稳。
- [ ] **端到端 RAG 评测**(faithfulness / context precision)。
- [ ] **再加横切能力**:Redis 缓存 → 安全 firewall(`aidefence`)→ MCP 接入。
- [ ] **L0 查询改写 / L5 答案合成**(LLM)。
- [ ] 视效果决定是否引入**混合路线**(对高价值垂直领域自建索引,降低外部依赖)。

---

## 参考来源

**Tavily 架构**
- [Tavily 101: AI-powered Search for Developers](https://www.tavily.com/blog/tavily-101-ai-powered-search-for-developers)
- [Tavily Search API Reference](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily — Introduction to Agentic search tool (Medium)](https://shankar-k.medium.com/tavily-introduction-to-agentic-search-tool-8720b9d6aa19)
- [How to Add Real-Time Web Search to Your LLM Using Tavily (freeCodeCamp)](https://www.freecodecamp.org/news/how-to-add-real-time-web-search-to-your-llm-using-tavily/)

**搜索源 API 对比**
- [Best Web Search APIs for AI Applications in 2026 (Firecrawl)](https://www.firecrawl.dev/blog/best-web-search-apis)
- [The best web search APIs for AI in 2026 (Brave)](https://brave.com/learn/best-search-api-2026/)
- [Bing Search API Alternatives 2026 (ScrapeGraphAI)](https://scrapegraphai.com/blog/bing-search-api-alternatives)
- [Beyond Tavily — Complete Guide to AI Search APIs in 2026](https://websearchapi.ai/blog/tavily-alternatives)
- [SerpApi vs. Brave Search API](https://serpapi.com/blog/serpapi-vs-brave-search-api/)

**正文抓取 / 提取**
- [Firecrawl vs Jina Reader 2026](https://use-apify.com/blog/firecrawl-vs-jina-reader-2026)
- [Heuristic vs. ML-Powered Extraction — Trafilatura vs. Jina ReaderLM](https://www.contextractor.com/trafilatura-vs-jina-readerlm/)
- [Jina AI vs. Firecrawl for web-LLM extraction (Apify)](https://blog.apify.com/jina-ai-vs-firecrawl/)
- [7 Best Web Scraping Tools for AI Agents 2026 (Fastio)](https://fast.io/resources/best-web-scraping-tools-ai-agents/)

**重排序**
- [Reranking & Cross-Encoders for RAG: BGE, Cohere, Jina (2026)](https://localaimaster.com/blog/reranking-cross-encoders-guide)
- [Open-source alternatives to Cohere Rerank in 2026 (ZeroEntropy)](https://zeroentropy.dev/articles/open-source-alternatives-to-cohere-rerank/)
- [The Critical Role of Rerankers in RAG (Medium)](https://medium.com/@akanshak/the-critical-role-of-rerankers-in-rag-98309f52abe5)
- [Top 7 Rerankers for RAG (Analytics Vidhya)](https://www.analyticsvidhya.com/blog/2025/06/top-rerankers-for-rag/)

> 注:搜索源/抓取类对比多来自厂商博客(Firecrawl、Brave、SerpAPI、ScrapeGraphAI 等),各自倾向自家产品。最终选型务必用**自己的真实查询**跑免费额度验证。
