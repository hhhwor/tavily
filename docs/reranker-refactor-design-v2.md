# Web / Academic / Patent Reranker 重构设计 v2(组合式方案)

> 对象: `src/pipeline/rerank.py` 中的 web、academic、patent 三类重排逻辑。
> 关系: 本文是 [reranker-refactor-design.md](./reranker-refactor-design.md)(以下简称 v1)的替代方案,解决同一组问题,但结构选择不同。v1 的问题诊断(§1-§2)本文直接复用,不重复展开。
> 日期: 2026-07-03

---

## 1. 与 v1 的分歧点

审阅 v1 时,在现有代码(`src/pipeline/rerank.py`、`src/engine.py`)里核实到三个和设计强相关的既有问题:

1. **专利已经在用错误信号排序**:`engine.py:_rank_patent` 复用的是 fusion 开启时的通用 `reranker`,其 `FusionReranker` 的 `authority`(按 `source` 查表,专利 `source="patent_es"` 不在表里,恒为默认 `0.8`)、`rank_signal`(按 `provider_rank`,专利恒为 `None`→恒为 `1.0`)对专利没有任何区分度,`freshness` 用的是 web 的一年线性衰减尺度套在专利 `application_date` 上。这在 `FUSION_ENABLED=true`(即 `tech-route-summary.md` 推荐的"质量优先"配置)时会实际发生,不是假设风险。
2. **共享可变状态导致的竞态**:`engine.py` 在并发重排前原地改写 `reranker._time_sensitive = plan.time_sensitive`,而 `reranker` 常是跨请求复用的单例(`_select_reranker` 全 `None` 时返回 `self.reranker`)。这是和 v1 §2.2 诊断的"`inner.rerank()` 原地副作用"同一类问题,只是发生在 engine 层。
3. **Key 冲突静默丢结果**:`AcademicReranker` 用 `{r.url: r for r in candidates}` 建索引,重复/空 `url` 时后者会静默覆盖前者,导致论文凭空消失,不只是排序问题。

v1 的分层(`DomainRerankerBase` 基类 + `WebReranker`/`AcademicReranker`/`PatentReranker` 子类 + `DomainScore` 九字段结构体)能解决"打分与领域策略耦合"的问题,但没有从结构上避免上面三类问题再次出现——比如 `DomainScore` 给三个领域共用同一组字段(`prior/freshness/authority/citations/venue/oa/status`),而没有一个领域会用满全部字段,这本身就是同一种"公共结构装了领域专属语义"的模式,只是把 `rerank_score` 的问题挪到了另一个共享结构上。

v2 的目标:用组合替代继承,用不可变 context 替代共享可变属性,让 patent 从一开始就和 web/academic 走同一条代码路径,而不是事后补的第四个分支。

---

## 2. 目标架构

```
SearchResult / AcademicResult / PatentResult
        │
        ▼
rerank_domain(query, candidates, config, ctx)   ← 一个纯函数,不是类继承树
        │
        ├─ key_fn(candidate) -> str              领域提供
        ├─ compress_fn(candidate) -> str          领域提供
        ├─ text_scorer.score(query, texts)        backend 提供,领域无关
        ├─ feature_fns: {name: candidate -> float} 领域提供
        ├─ weight_fn(query, ctx) -> {name: float}  领域提供
        ├─ blend: final = text_w·text + Σ w_i·feature_i
        └─ stable_sort(key=(-final, tiebreakers))
        │
        ▼
SearchResult.rerank_score = final
```

`WebReranker`/`AcademicReranker`/`PatentReranker` 不再是三个并列子类,而是三份 `DomainConfig` 配置 + 各自的薄封装函数,真正的排序逻辑只有一份实现(`rerank_domain`),三个领域的差异**只**体现在传入的配置里,方便逐字段对比,也方便共用同一套契约测试。

---

## 3. 核心接口

### 3.1 TextScorer —— 纯文本打分,不认识任何领域模型

```python
class TextScorer(Protocol):
    name: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """按输入顺序返回每个文本的相关性分,0.0-1.0。不排序、不截断、不改输入。"""
        ...
```

与 v1 的关键差异:签名是 `texts: Sequence[str]`,不是 `Sequence[SearchResult]`。backend 完全不感知 `SearchResult`/`AcademicResult`/`PatentResult` 的存在,把"文本压缩"完全推给调用方(领域配置里的 `compress_fn`)。这样 backend 层的测试只需要构造字符串列表,不需要构造任何领域模型。

**线程安全是接口契约,不是迁移期风险清单里的一项**:本地 CrossEncoder 类实现(BGE/FlashRank)在构造函数里自带一把 `threading.Lock`,包住 `predict`/`rerank` 调用,对外表现为"这个方法本来就是线程安全的",调用方(engine 并发跑 web/academic/patent 三路)不需要知道、也不需要额外处理。SiliconFlow 这类远程 API scorer 天然无状态,不需要锁。

```python
class BGETextScorer(TextScorer):
    def __init__(self, ...):
        self._model = CrossEncoder(...)
        self._lock = threading.Lock()

    def score(self, query, texts):
        with self._lock:
            return self._model.predict([(query, t) for t in texts]).tolist()
```

### 3.2 Scored —— 打分结果,feature 是开放的 dict,不是固定字段的大结构体

```python
@dataclass
class Scored:
    key: str
    text: float                      # 唯一保证所有领域都有的字段
    features: dict[str, float]       # 领域自定义,如 {"citations": .., "venue": ..}
    passed_threshold: bool = False
    final: float = 0.0
```

不用 v1 的 `DomainScore`(固定 `prior/freshness/authority/citations/venue/oa/status` 九字段)。`features` 是开放字典,每个领域只填自己用得到的 key——`WebReranker` 填 `{"prior": ...}`,`AcademicReranker` 填 `{"citations":.., "freshness":.., "venue":.., "oa":..}`,`PatentReranker` 填 `{"source_score":.., "freshness":.., "citations":.., "status":..}`。没有哪个领域需要面对"这个字段对我没意义但结构体里必须留着"的情况。

代价:`features` 字典失去了 dataclass 字段的静态类型检查,拼错 key 不会在类型检查阶段报错。缓解办法是每个领域配置里把用到的 feature name 定义成模块级常量(`CITATIONS = "citations"`),而不是裸字符串散落各处;契约测试里也会校验 `weight_fn` 返回的 key 集合和 `feature_fns` 的 key 集合完全一致。

### 3.3 RerankContext —— 不可变的请求上下文,取代原地改写共享属性

```python
@dataclass(frozen=True)
class RerankContext:
    time_sensitive: bool = False
    wants_recent: bool = False
    wants_foundational: bool = False
```

`engine.py` 现状是 `reranker._time_sensitive = plan.time_sensitive` 原地改写单例属性。v2 里 `RerankContext` 由 `plan_query()` 的产出派生,随每次 `rerank_domain()` 调用显式传入,`weight_fn(query, ctx)` 据此返回权重表。reranker 实例本身不持有任何请求相关的可变字段,默认单例可以安全地被多个并发请求复用,不需要"这个 reranker 不能是共享单例"这种隐含约束。

### 3.4 DomainConfig —— 领域差异的唯一载体

```python
@dataclass
class DomainConfig:
    name: str
    key_fn: Callable[[Any], str]
    compress_fn: Callable[[Any], str]
    feature_fns: dict[str, Callable[[Any], float]]
    weight_fn: Callable[[str, RerankContext], dict[str, float]]   # 含 "text" 权重
    threshold: float = 0.3
    max_docs: Optional[int] = None    # 打分前的候选上限(academic 场景需要)
    prepare_fn: Optional[Callable[[list], list]] = None  # 候选准备,如 web 的 rrf_fuse + 稳定排序
```

### 3.5 rerank_domain —— 唯一的排序实现

```python
def rerank_domain(
    query: str,
    candidates: list[T],
    config: DomainConfig,
    text_scorer: TextScorer,
    ctx: RerankContext,
    top_k: int,
) -> list[T]:
    if not candidates:
        return []
    prepared = config.prepare_fn(candidates) if config.prepare_fn else candidates
    pool = prepared[: config.max_docs] if config.max_docs else prepared

    texts = [config.compress_fn(c) for c in pool]
    text_scores = text_scorer.score(query, texts)

    weights = config.weight_fn(query, ctx)
    scored: list[Scored] = []
    for c, text in zip(pool, text_scores):
        features = {name: fn(c) for name, fn in config.feature_fns.items()}
        final = weights.get("text", 1.0) * text + sum(
            weights.get(name, 0.0) * val for name, val in features.items()
        )
        scored.append(Scored(
            key=config.key_fn(c), text=text, features=features,
            passed_threshold=text >= config.threshold, final=final,
        ))

    for c, s in zip(pool, scored):
        c.rerank_score = s.final   # 唯一一处写 rerank_score,且只写最终分

    order = sorted(range(len(pool)), key=lambda i: -scored[i].final)
    return [pool[i] for i in order][:top_k]
```

`rerank_score` 只在这一处、只被赋值一次、只表示最终领域分——直接解决 v1 §2.1 诊断的"同一字段反复改写"问题,因为整个流水线里不再有第二个地方会写它。

`key_fn` 统一处理 v1 §13.2 提到的 key 稳定性问题,并且**必须保证唯一性**:

```python
def academic_key(p: AcademicResult, idx: int) -> str:
    return p.doi or p.url or f"{p.title}|{p.year}|{idx}"   # idx 兜底,避免重复/空 key 覆盖
```

契约测试里会专门构造重复/空 key 的候选集,断言排序前后候选数量不变——不是排序正确,是"不丢东西"。

---

## 4. 三个领域的配置(全部是数据,不是子类)

### 4.1 Web

```python
def build_web_config(threshold: float) -> DomainConfig:
    return DomainConfig(
        name="web",
        key_fn=lambda r: r.url,
        compress_fn=_compress_web_text,          # title + snippet + content(前缀去重)
        feature_fns={"prior": lambda r: r.rrf_prior},   # RRF 分预先算好挂在候选上,见下
        weight_fn=lambda q, ctx: {"text": 0.85, "prior": 0.15},
        threshold=threshold,
        prepare_fn=_stable_rrf_fuse,              # rrf_fuse + 稳定排序,RRF 分存 rrf_prior 而非 rerank_score
    )
```

RRF 分不再借用 `rerank_score` 传递(修复 v1 §7.1 已经指出的问题),而是 `prepare_fn` 阶段给每个候选写一个专用的 `rrf_prior` 属性(`SearchResult` 加一个可选字段,或用外部 `dict[key, float]` 传递,不污染模型)。`pass_bonus` 的语义并入 `feature_fns`:`{"pass_bonus": lambda r: 1.0 if text_score(r) >= threshold else 0.0}`——不过这需要 feature 函数能访问 text_score,实现上更自然的做法是把 `pass_bonus` 直接留在 `rerank_domain` 通用逻辑里作为可选项(`config.pass_bonus: float = 0.0`),而不是塞进 `feature_fns`,因为它依赖的是 `text`/`threshold` 这两个跨领域通用量,不是领域专属信号。

### 4.2 Academic

```python
def build_academic_config(threshold: float) -> DomainConfig:
    return DomainConfig(
        name="academic",
        key_fn=lambda p, i: p.doi or p.url or f"{p.title}|{p.year}|{i}",
        compress_fn=lambda p: f"{p.title}\n{p.content or p.snippet}"[:480],
        feature_fns={
            "citations": lambda p: math.log1p(max(0, p.citations)),
            "freshness": lambda p: _academic_freshness(p),   # 闭包里读不到 ctx,freshness 用 wants_recent 预先分两个变体传入
            "venue": _venue_score,
            "oa": _oa_score,
        },
        weight_fn=_academic_weights,   # 按 wants_recent/wants_foundational 返回四组权重之一,复用 v1 §8.4 的数值
        threshold=threshold,
        max_docs=25,
    )
```

不再有"threshold 过滤后回填 leftovers"的逻辑(v1 §8.5 已经指出这点,v2 里更彻底——`rerank_domain` 从来不会因为 `passed_threshold=False` 丢候选,`threshold` 只写进 `Scored.passed_threshold` 字段供调试/日志用,不影响 `top_k` 截断前的候选集合)。

> 注:`feature_fns` 里的函数目前签名是 `Callable[[T], float]`,但 `freshness` 需要感知 `ctx.wants_recent`。处理方式:`weight_fn`/`feature_fns` 的构建函数(`build_academic_config`)接收 `ctx` 作为闭包外部变量提前构造,或者把 `feature_fns` 的签名放宽成 `Callable[[T, RerankContext], float]`。后者更清晰,写入本文档定稿版本时按 `Callable[[T, RerankContext], float]` 统一签名,避免闭包传值的隐晦写法。

### 4.3 Patent —— 和 web/academic 同批次实现,不是事后加的第四个分支

```python
def build_patent_config(threshold: float) -> DomainConfig:
    return DomainConfig(
        name="patent",
        key_fn=lambda p, i: p.publication_number or p.url or f"{p.title}|{p.application_number}|{i}",
        compress_fn=_compress_patent_text,   # title + abstract + applicant + ipc/cpc + publication_number
        feature_fns={
            "source_score": lambda p, ctx: p.score or 0.0,   # ES _score,需 normalize_list 归一化,由 prepare_fn 处理
            "freshness": _patent_freshness,    # 优先 publication_date,缺失退 application_date
            "citations": lambda p, ctx: math.log1p(max(0, p.citation_count)),
            "status": _status_score,           # active/granted/pending/published=1.0, unknown=0.5, expired=0.2
        },
        weight_fn=_patent_weights,
        threshold=threshold,
        prepare_fn=_normalize_patent_source_score,
    )
```

数值权重直接复用 v1 §9.4 的建议值,这部分设计文档层面没有分歧。关键差异是 engine 侧不再有"web/academic 有专用 reranker、patent 走 generic reranker"的不对称代码路径——三个 `build_*_config()` 从 Phase 1 起就一起接入 engine,`_rank_patent` 不会在过渡期内继续借用 fusion 版通用 reranker。

---

## 5. Engine 接入

```python
def _rank_web(raw, ctx) -> list[SearchResult]:
    return rerank_domain(search_query, raw, web_config, text_scorer, ctx, top_k)

def _rank_academic(papers, ctx) -> list[AcademicResult]:
    return rerank_domain(academic_query, papers, academic_config, text_scorer, ctx, top_k)

def _rank_patent(patents, ctx) -> list[PatentResult]:
    return rerank_domain(search_query, patents, patent_config, text_scorer, ctx, top_k)
```

`text_scorer` 三路共用同一个只读实例(本身线程安全,见 §3.1),`ctx = RerankContext(time_sensitive=plan.time_sensitive, wants_recent=..., wants_foundational=...)` 每次请求现算现传,不写回任何共享对象。`ThreadPoolExecutor` 并发跑三个闭包时,不存在任何跨闭包共享可变状态——这是直接针对审阅里发现的 `reranker._time_sensitive` 竞态设计的解法。

---

## 6. 迁移计划(调整后的顺序)

v1 把 Patent 排在 Phase 4,理由是"先小改动验证结构,再新增领域"。v2 建议反过来:因为组合式设计下 patent 配置的实现成本和 web/academic 相当(不再是"多写一个继承体系"),而 patent 当前处于"配置一变就更差"的潜伏 bug 状态,更应该优先解决。

| Phase | 内容 | 验收 |
|-------|------|------|
| 1 | `TextScorer`/`Scored`/`RerankContext`/`DomainConfig`/`rerank_domain` 落地;本地 backend 加锁 | 契约测试(§7)全绿;backend 单测覆盖全候选打分 |
| 2 | 三个 `build_*_config()` 同时接入 engine,替换 `WebReranker`/`AcademicReranker`/旧 patent 路径 | `results`/`academic_results`/`patent_results` 三路输出的 NDCG 相对旧实现不回归(web/academic 应完全一致,因为公式未变;patent 是新增,单独跑一次评测) |
| 3 | 清理:删除 `WebReranker`/`AcademicReranker`/`FusionReranker`/`ThresholdReranker` 旧类,`build_reranker()` 精简为 `build_text_scorer()` + 三个 `build_*_config()` | 无遗留死代码;`grep rerank_score` 只在 `rerank_domain` 里出现一次赋值 |

不再单独设置"Phase: 重构 WebReranker"/"Phase: 重构 AcademicReranker"两个阶段,因为组合式设计下两者的改动量和风险都显著小于 v1 的继承重写,合并进一个 Phase 更符合实际工作量。

---

## 7. 测试策略:共享契约测试,而不是三份重复断言

v1 的 `test_web_reranker.py`/`test_academic_reranker.py` 各自手写了一遍"threshold 不截断"/"scorer 不改输入仍能排序"之类的断言。v2 因为三个领域共用同一个 `rerank_domain()` 实现,可以把这类断言写成一份参数化契约测试,对三个 `DomainConfig` 都跑一遍:

```python
@pytest.mark.parametrize("config_factory", [build_web_config, build_academic_config, build_patent_config])
def test_domain_reranker_scores_all_candidates(config_factory):
    ...

@pytest.mark.parametrize("config_factory", [...])
def test_domain_reranker_does_not_mutate_input_when_scorer_is_pure(config_factory):
    ...

@pytest.mark.parametrize("config_factory", [...])
def test_domain_reranker_handles_duplicate_or_empty_keys_without_dropping_candidates(config_factory):
    ...

@pytest.mark.parametrize("config_factory", [...])
def test_domain_reranker_concurrent_calls_do_not_interfere(config_factory):
    ...
```

领域专属的行为(citations 方向、venue 打分、patent status 映射)仍然各自写测试,但通用契约不再三份重复维护——这也是组合式设计相对继承式设计的一个直接收益:公共行为只有一份实现,自然只需要一份契约测试。

---

## 8. 风险与权衡(相对 v1 的取舍)

- **可读性**:配置字典 + 纯函数比"类继承 + 方法重写"更紧凑,但对习惯面向对象的读者,函数式风格的可读性和 IDE 跳转体验不如显式类。如果团队更看重"每个领域是一个明确的类,方便单独 mock",v1 的继承方案也可接受,但要吸取本文档 §3.2 的教训,把 `DomainScore` 从固定九字段结构体改成开放 `features: dict`,避免同一种"公共结构装专属语义"的问题重演。
- **`feature_fns`/`weight_fn` 的 key 一致性靠约定,不是类型系统**:字符串 key 拼错不会在类型检查时报错。缓解:模块级常量 + 契约测试里校验 `weight_fn` 返回 key 集合与 `feature_fns` key 集合一致。
- **`compress_fn`/`key_fn` 签名要带 index**:见 §3.5,`key_fn` 需要 `idx` 兜底避免空/重复 key 覆盖,`rerank_domain` 内部调用时要传 `enumerate`,不能简单 `map(config.key_fn, pool)`。

---

## 9. 完成态

```text
backend scorer(与领域无关,只认字符串):
  SiliconFlowTextScorer / BGETextScorer / FlashRankTextScorer / NoOpTextScorer

领域配置(数据,不是类):
  build_web_config() / build_academic_config() / build_patent_config()

唯一排序实现:
  rerank_domain(query, candidates, config, text_scorer, ctx, top_k)

engine:
  web / academic / patent 三路 → 同一个 rerank_domain(),只是 config 不同
```

判断标准(与 v1 一致,复用):

- `rerank_score` 只在 `rerank_domain` 内被赋值一次,表示最终分。
- 三个领域共用同一份排序实现和同一套契约测试,领域差异只体现在 `DomainConfig` 数据里。
- reranker/backend 实例不持有任何跨请求可变状态;并发调用不需要额外协调。
- patent 从 Phase 1/2 起就和 web/academic 处于同等地位,不存在"临时借用其他领域 reranker"的过渡态。
