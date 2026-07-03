# Web / Academic / Patent Reranker 重构设计

> 对象: `src/pipeline/rerank.py` 中的 web、academic、patent 三类重排逻辑。
> 目标: 把重排 backend 的文本相关性打分,和不同领域的业务排序策略解耦。
> 日期: 2026-07-03

本文是重构方案文档,不描述当前已落地状态。当前实现快照见 [tech-route-summary.md](./tech-route-summary.md)。

---

## 1. 背景

当前重排代码已经形成三类不同使用场景:

- **Web**: 多搜索源召回,需要跨源去重、RRF prior、多源稳定排序,再用文本相关性精排。
- **Academic**: 单独论文结果块,需要融合文本相关性、引用数、新鲜度、venue、OA 可访问性。
- **Patent**: 单独专利结果块,目前直接复用通用 reranker,但专利有 ES `_score`、申请/公开日期、引用数、法律状态、申请人、分类号等结构化信号。

这三类场景的共性是:都需要 query-document 的文本相关性分数。差异是:候选准备、文本压缩、结构化特征、最终融合公式、回填策略都不同。

因此重构方向是:把 **backend scorer** 和 **domain reranker** 分开。

---

## 2. 当前问题

### 2.1 `rerank_score` 语义混用

当前 `rerank_score` 至少承载三种含义:

- RRF 融合分: `rrf_fuse()` 把跨源 prior 写入 `rerank_score`。
- 文本相关性分: BGE / FlashRank / SiliconFlow / ThresholdReranker 把 cross-encoder 分写入 `rerank_score`。
- 最终领域融合分: WebReranker / AcademicReranker 再覆盖为最终分。

同一个字段在同一条 pipeline 中反复改写,导致调试、测试和新增特征都不清楚当前分数代表什么。

### 2.2 依赖 `inner.rerank()` 的原地副作用

`WebReranker` 和 `AcademicReranker` 都假设 `inner.rerank()` 会给传入的候选对象原地写入 `rerank_score`。即使结果被 threshold 过滤掉,外层仍希望从原始对象上读到文本分。

这个约定没有体现在 `Reranker` 接口中。以后换 backend 或改 ThresholdReranker 时,很容易破坏 web/academic 的最终排序。

### 2.3 threshold 职责不清

通用 `ThresholdReranker` 当前会直接过滤结果。但 web 和 academic 实际上不希望 threshold 决定最终条数:

- Web 需要全候选有文本分,threshold 只作为强通过信号。
- Academic 会在 threshold 过滤后做回填,避免最终论文数量不足。
- Patent 后续也更适合把 threshold 作为特征,而不是全局截断规则。

### 2.4 领域策略分散且重复

`WebReranker` 和 `AcademicReranker` 都有自己的 normalize、fallback、回填和稳定排序逻辑。继续新增 `PatentReranker` 会复制更多类似代码。

### 2.5 engine 中领域边界不完整

当前 web 和 academic 已经有专用 reranker,patent 仍然走通用 reranker。专利排序没有利用专利结构化字段,也没有形成和 academic 平行的领域策略。

---

## 3. 设计目标

1. **显式打分**: backend 返回显式文本分,不依赖 SearchResult 原地副作用。
2. **领域解耦**: backend 只管文本相关性,domain reranker 负责领域排序。
3. **行为可迁移**: 第一阶段保留现有 web 和 academic 排序语义,只调整结构。
4. **Patent 独立化**: 新增 PatentReranker,不再把专利当普通 SearchResult 处理。
5. **中间分可解释**: RRF prior、text score、freshness、citations 等特征在内部结构中分开保存。
6. **测试边界明确**: backend scorer、领域融合、engine 分支分别测试。

---

## 4. 目标架构

```
SearchResult / AcademicResult / PatentResult
        │
        ▼
DomainReranker
        │
        ├─ prepare_candidates()
        ├─ compress_for_scoring()
        ├─ TextScorer.score_all()
        ├─ compute_features()
        ├─ blend_scores()
        └─ stable_sort()
        │
        ▼
SearchResult.rerank_score = final_score
```

分层后职责如下:

| 层 | 职责 | 不负责 |
|----|------|--------|
| TextScorer | query-document 文本相关性打分 | threshold 截断、RRF、引用数、时间、来源权重 |
| DomainRerankerBase | 通用流程、归一化、稳定排序、回填 | 领域公式细节 |
| WebReranker | RRF prior + 文本分融合 | 学术引用/专利法律状态 |
| AcademicReranker | 文本 + citation + freshness + venue + OA | web 多源 RRF |
| PatentReranker | 文本 + ES score + 日期 + 引用 + 状态等 | web/academic 特征 |

---

## 5. 核心接口

### 5.1 TextScore

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class TextScore:
    key: str
    score: float
    passed: bool
```

字段说明:

- `key`: 文档稳定 key,通常使用 URL 或规范化 URL。
- `score`: 归一化后的文本相关性分,范围建议保持 `0.0 <= score <= 1.0`。
- `passed`: 是否超过当前 threshold。该字段只表示强通过信号,不直接决定 domain reranker 最终条数。

### 5.2 TextScorer

```python
class TextScorer(Protocol):
    name: str

    def score_all(
        self,
        query: str,
        results: Sequence[SearchResult],
    ) -> list[TextScore]:
        ...
```

`score_all()` 的约束:

- 必须尽量给每个输入候选返回分数。
- 不排序、不截断、不做领域融合。
- 不要求修改输入对象。
- backend 失败时由构建层回退到 `NoOpTextScorer`。

兼容期可以保留现有 `Reranker.rerank()`:

```python
class Reranker(ABC):
    def rerank(...):
        ...

    def score_all(...):
        # 默认通过 rerank() 兼容旧实现,迁移完成后反向收敛。
        ...
```

但长期接口应以 `score_all()` 为主。

### 5.3 DomainScore

```python
@dataclass
class DomainScore:
    key: str
    text: float = 0.0
    prior: float = 0.0
    freshness: float = 0.0
    authority: float = 0.0
    source_score: float = 0.0
    citations: float = 0.0
    venue: float = 0.0
    oa: float = 0.0
    status: float = 0.0
    passed_text_threshold: bool = False
    final: float = 0.0
```

`DomainScore` 是内部结构,不直接暴露到 API。最终仍只把 `final` 写回 `SearchResult.rerank_score`。

---

## 6. DomainRerankerBase

建议抽一个模板基类,统一执行流程:

```python
class DomainRerankerBase(Reranker):
    def rerank(self, query: str, results: list[SearchResult], top_k: int) -> list[SearchResult]:
        candidates = self.prepare_candidates(results)
        scoring_inputs = [self.compress_for_scoring(r) for r in candidates]
        text_scores = self.text_scorer.score_all(query, scoring_inputs)
        domain_scores = self.compute_features(query, candidates, text_scores)
        self.blend_scores(query, candidates, domain_scores)
        return self.select_top_k(candidates, domain_scores, top_k)
```

公共工具可以放在这个基类或同文件 helper 中:

- `clamp01(value)`
- `normalize_list(values, default=0.0)`
- `rank_fallback(n)`
- `score_by_key(scores)`
- `stable_result_key(result)`
- `parse_date_days_ago(date_str)`
- `stable_sort(results, scores)`

这个基类不需要一次性抽到很重。第一阶段只抽 web/academic/patent 真正共享的部分,避免过度抽象。

---

## 7. WebReranker 设计

### 7.1 输入与候选准备

Web 的输入来自多搜索源,同一 URL 可能被不同 provider 召回。候选准备应继续保留当前语义:

```text
raw web results
→ rrf_fuse(results, top_k=None)
→ stable sort by (-rrf_score, provider_rank, url)
```

RRF 分不要长期存放在 `rerank_score`;可以在候选准备阶段写入内部 `prior_by_key`。

### 7.2 文本压缩

当前压缩策略是 `title + snippet + content` 截断。建议做两点调整:

- 如果 `content` 已包含或以 `snippet` 开头,不要重复拼接 snippet。
- 保留标题优先级,让标题不被正文挤掉。

建议伪代码:

```python
parts = [title]
if snippet:
    parts.append(snippet)
if content and snippet not in content[: max(len(snippet) + 80, 120)]:
    parts.append(content)
```

### 7.3 最终融合

第一阶段保留当前公式:

```text
final = 0.85 * text + 0.15 * normalized_rrf + pass_bonus
```

其中:

- `text`: TextScorer 返回的文本分。
- `normalized_rrf`: RRF prior 的 min-max 归一化结果。
- `pass_bonus`: 文本分超过 threshold 时给的小奖励,默认保持 `0.02`。

排序保持稳定:

```text
sort by (-final, provider_rank or INF, url)
```

### 7.4 Web 行为约束

必须保留:

- 给全候选打文本分,不因为 threshold 导致最终条数不足。
- 文本分明显差异时,文本分主导 RRF prior。
- 文本分持平时,RRF prior 起稳定排序作用。
- 不受 provider 完成顺序影响。

---

## 8. AcademicReranker 设计

### 8.1 候选范围

继续保留 `max_docs` 限制,避免长摘要和大候选集导致重排成本不可控。

```text
papers = academic_results[:max_docs]
```

### 8.2 文本压缩

论文场景应优先保留:

- title
- abstract
- venue / year 可考虑作为轻量上下文,但第一阶段不强制加入文本打分输入

当前 `title + abstract` 可保留。

### 8.3 特征

Academic 的特征保持当前语义:

- `text`: TextScorer 分数。
- `citations`: `log1p(citations)` 后归一化。
- `freshness`: 对 recent/latest 查询加重新鲜度;普通查询用更缓的年级别衰减。
- `venue`: 正式 venue 高于 arXiv/bioRxiv/SSRN 等预印本。
- `oa`: PDF 直链最高,OA landing 或 `is_oa` 次之。

### 8.4 权重

保留 query-aware 权重:

```text
recent + foundational: text 0.70, citations 0.13, freshness 0.10, venue 0.04, oa 0.03
recent only:           text 0.72, citations 0.08, freshness 0.14, venue 0.04, oa 0.02
foundational only:     text 0.70, citations 0.20, freshness 0.04, venue 0.04, oa 0.02
default:               text 0.76, citations 0.12, freshness 0.06, venue 0.04, oa 0.02
```

### 8.5 回填策略

Academic 不应因为 threshold 过滤导致最终结果少于 `top_k`。迁移后不需要手工回填 filtered leftovers,因为 `score_all()` 会返回全候选文本分。最终直接对全部候选计算领域分并排序。

---

## 9. PatentReranker 设计

### 9.1 新增独立领域 reranker

Patent 应和 Academic 平行,作为单独的领域 reranker 接入 engine:

```python
patent_text_scorer = self._select_text_scorer(...)
patent_reranker = PatentReranker(patent_text_scorer)
```

`engine._rank_patent()` 不再直接调用通用 `reranker.rerank(search_query, patents, top_k)`。

### 9.2 文本压缩

专利文本打分输入建议包括:

- `title` / `patent_name`
- `snippet` / `abstract`
- `applicant` 前几个
- `ipc_main` / `cpc_main`
- `publication_number`

目标不是把所有元数据塞进 cross-encoder,而是给模型足够上下文区分同名或相近专利。

建议格式:

```text
{title}
摘要: {abstract}
申请人: {applicant1}; {applicant2}
分类: {ipc_main or cpc_main}
公开号: {publication_number}
```

### 9.3 特征

第一阶段建议使用以下特征:

- `text`: TextScorer 分数,主导排序。
- `source_score`: 专利 ES `_score` 归一化,作为召回系统先验。
- `freshness`: 优先用 `publication_date`,缺失时用 `application_date`。
- `citations`: `log1p(citation_count)` 后归一化。
- `status`: 法律状态轻量加权,默认未知给中性分。

### 9.4 默认权重

建议初始权重:

```text
default:
  text         0.72
  source_score 0.12
  freshness    0.06
  citations    0.06
  status       0.04

recent query:
  text         0.70
  source_score 0.10
  freshness    0.12
  citations    0.04
  status       0.04
```

法律状态初始映射建议保守:

```text
active / granted / pending / published: 1.0
unknown / empty:                         0.5
expired / withdrawn / abandoned:         0.2
```

如果实际 `status` 字段质量不稳定,第一阶段可以把 `status` 权重置为 `0.0`,只保留代码结构。

### 9.5 Patent 行为约束

- NoOp 或 scorer 不可用时,按 ES `_score` 降序兜底。
- 不和 web 结果混排,仍写入 `patent_results`。
- 不使用 web 的 RRF prior。
- 不使用 academic 的 venue / OA 逻辑。

---

## 10. 构建与命名

当前 `build_reranker()` 同时承担 backend 构建、threshold 包装、fusion 包装。重构后建议拆成:

```python
def build_text_scorer(...) -> TextScorer:
    ...

def build_web_reranker(text_scorer: TextScorer, ...) -> WebReranker:
    ...

def build_academic_reranker(text_scorer: TextScorer, ...) -> AcademicReranker:
    ...

def build_patent_reranker(text_scorer: TextScorer, ...) -> PatentReranker:
    ...
```

兼容期可以保留 `build_reranker()`:

```python
def build_reranker(...) -> Reranker:
    scorer = build_text_scorer(...)
    return GenericTextReranker(scorer)
```

长期建议:

- API 参数继续叫 `rerank_backend`,避免接口破坏。
- 内部类名从 `BGEReranker` 等逐步迁移为 `BGETextScorer`。
- `ThresholdReranker` 改为 `ThresholdTextScorer` 或合并进 scorer 配置。
- `FusionReranker` 标记为 legacy,不再作为 web/academic/patent 主路径。

---

## 11. 迁移计划

### Phase 1: 引入显式文本打分

目标: 最小化行为变化。

- 新增 `TextScore`。
- 给现有 backend 增加 `score_all()`。
- `rerank()` 继续保留,内部可调用 `score_all()` 后排序/过滤。
- `ThresholdReranker` 改为在 `TextScore.passed` 上表达 threshold,兼容旧 `rerank()` 过滤行为。

验收:

- 现有 web/academic/siliconflow truncate 测试通过。
- backend scorer 单测覆盖全候选打分。

### Phase 2: 重构 WebReranker

目标: 去掉 WebReranker 对 inner 原地副作用的依赖。

- WebReranker 改用 `TextScorer.score_all()`。
- RRF prior 存入内部 map,不借用 `rerank_score` 传递。
- 保留当前公式和稳定排序。
- 改善 `_compress()` 去重 snippet/content。

验收:

- `tests/test_web_reranker.py` 全部通过。
- 新增测试: mock scorer 不修改输入对象时,WebReranker 仍能正确排序。

### Phase 3: 重构 AcademicReranker

目标: 使用同一套显式 score path。

- AcademicReranker 改用 `TextScorer.score_all()`。
- 移除 threshold leftovers 回填逻辑,改为全候选领域打分。
- 保留当前 query-aware 权重。

验收:

- `tests/test_academic_reranker.py` 全部通过。
- 新增测试: scorer 返回全候选分数但 passed=false 时,仍可按领域分填满 top_k。

### Phase 4: 新增 PatentReranker

目标: 专利排序独立成领域策略。

- 新增 `PatentReranker`。
- `engine._rank_patent()` 接入 PatentReranker。
- 增加专利字段压缩和特征融合。

验收:

- 新增 `tests/test_patent_reranker.py`。
- 覆盖 ES score 兜底、文本分主导、recent query 新鲜度加权、status 缺失中性处理。

### Phase 5: 清理构建链

目标: 构建逻辑更清晰。

- 新增 `build_text_scorer()`。
- `build_reranker()` 保留兼容入口。
- `FusionReranker` 标记 legacy 或只保留通用 SearchResult fallback。
- engine 中不再手动传 `fusion=False` 来绕过通用 FusionReranker。

---

## 12. 测试策略

### 12.1 单元测试

Web:

- 全候选均被 scorer 看到。
- threshold 不直接截断最终结果。
- 文本分主导 RRF prior。
- 文本分持平时 RRF prior 生效。
- provider 完成顺序不影响输出。
- scorer 不修改输入对象时仍正确。

Academic:

- citations / freshness / venue / OA 权重方向正确。
- recent query 提高 freshness 权重。
- foundational query 提高 citations 权重。
- threshold 未通过的候选仍可参与最终领域排序。

Patent:

- NoOp scorer 时按 ES `_score` 兜底。
- 文本分显著更高时能压过 ES 先验。
- recent query 增强 publication/application date。
- citation_count 经过 log 归一化。
- status 缺失不惩罚过重。

Backend scorer:

- SiliconFlow 不做本地 25 条截断。
- 本地 backend 分数归一化到 0-1。
- 空文本候选不抛错。
- chunk max-pooling 行为保持。

### 12.2 集成测试

- web only: 返回 `results`。
- academic only / forced: 返回 `academic_results`。
- patent only / forced: 返回 `patent_results`。
- web + academic + patent 并发重排时不共享可变 scorer 状态。

### 12.3 评测

重构后需要跑现有 eval,重点比较:

- web top-k 相关性是否回归。
- academic 论文排序是否回归。
- patent top-k 是否比通用 reranker 更稳定。
- latency 是否因为全候选显式打分增加。

---

## 13. 风险与处理

### 13.1 scorer 线程安全

engine 当前会并发跑 web / academic / patent。若多个 domain reranker 共享同一个本地 CrossEncoder scorer,可能遇到线程安全问题。

处理建议:

- SiliconFlow API scorer 可共享。
- 本地 BGE / FlashRank scorer 初期加锁保护 `predict/rerank`。
- 后续如有性能需求,再按 backend 类型配置 scorer pool。

### 13.2 URL 作为 key 不总是稳定

Academic 和 Patent 有些结果可能 URL 为空或合成 URL。需要统一 `stable_result_key()`:

```text
Academic: doi or url or title+year
Patent: publication_number or url or title+application_number
Web: normalize_url(url) or url
```

### 13.3 分数可比性

不同 backend 的文本分分布不完全一致。短期保留现有归一化策略:

- SiliconFlow: 使用 API 的 `relevance_score`。
- BGE/FlashRank: sigmoid 归一化。

长期可考虑 per-query min-max 或 calibration,但不作为本轮重构目标。

### 13.4 行为变更过大

第一阶段只改结构,不改 web/academic 权重。Patent 是新增领域策略,可以单独评估。

---

## 14. 完成态

完成重构后,代码关系应变为:

```text
backend scorer:
  SiliconFlowTextScorer
  BGETextScorer
  FlashRankTextScorer
  NoOpTextScorer

domain reranker:
  WebReranker(TextScorer)
  AcademicReranker(TextScorer)
  PatentReranker(TextScorer)

engine:
  web      -> WebReranker
  academic -> AcademicReranker
  patent   -> PatentReranker
```

判断标准:

- `rerank_score` 只在最终结果阶段表示最终分。
- domain reranker 不依赖 backend 原地修改 SearchResult。
- threshold 是文本强通过信号,不是所有领域的统一截断规则。
- web / academic / patent 的领域特征各自清晰,互不污染。
- 现有测试通过,新增 patent 测试覆盖领域策略。
