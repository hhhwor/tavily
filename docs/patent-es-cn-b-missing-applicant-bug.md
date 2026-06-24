# Bug 报告:专利 ES `epo_docdb_v2` 中国授权(B)文献 `applicant`/`inventor` 字段大面积缺失

> ✅ **已修复(2026-06-21)**:候选索引 `epo_docdb_v2_20260620`(读别名 `epo_docdb_read`)已修复该问题——
> 申请人覆盖率 80.8%→90.1%、发明人 76.1%→85.8%,并新增**中文原文名**可检索(0→55.5M)。当事人字段结构
> 改为 object `{original, docdb, docdba}`。本项目已切到该别名并适配字段(见 `src/providers/patent_es.py`
> `_extract_names`)。下文为修复前的原始报告,存档备查。

- **提报日期**:2026-06-17
- **集群**:`https://search.houdutech.cn:9243`(只读专利 ES)
- **索引**:`epo_docdb_v2_20260615`(EPO DOCDB,~1.72 亿)
- **严重度**:中(影响中国专利的检索可用性与当事人召回,但不影响服务可用性)
- **报告人**:chukonu-web-search 接入方

---

## 1. 一句话

中国**授权公告(`patent_type=B`)** 文献的 `applicant`(申请人)与 `inventor`(发明人)字段**大面积缺失**(覆盖率仅 ~13%),而同一专利族的**申请公开(`patent_type=A`)** 文献这两个字段是齐全的(~98%)。怀疑是入库/著录解析环节对 CN-B 文献漏抽当事人,而非 DOCDB 源数据本身缺失。

## 2. 现象 / 影响

- 按中文标题/摘要检索时,命中结果以 CN-B 授权文献为主(中文分词字段匹配强),这些记录的 `applicant`/`inventor` 多为**字段不存在**(不是空串、不是 null,是 `_source` 里根本没有该 key)。
- 下游(如「按申请人/发明人归并、当事人展示、专利权人分析」)对中国专利基本拿不到当事人,中国专利检索体验明显劣于美/欧/日。

## 3. 复现步骤(可直接 curl,只读)

### 3.1 同一专利族 A vs B 对照(最直接的证据)

```bash
curl -s 'https://search.houdutech.cn:9243/epo_docdb_v2_20260615/_search' \
  -H 'Content-Type: application/json' -d '{
    "size": 10,
    "query": {"term": {"family_id": "71196827"}},
    "_source": ["publication_number","patent_type","applicant","inventor"]
  }'
```

实际返回(同一件发明,family_id=71196827):

| 公开号 | patent_type | applicant | inventor |
|---|---|---|---|
| `CN-111354924-A` | A(申请公开) | `SHENZHEN INST ADV TECH` | `TANG YONGBING; SONG TIANYI; YAO WENJIAO` |
| `CN-111354924-B` | B(授权公告) | **(字段缺失)** | **(字段缺失)** |

→ 同族 A 文献当事人齐全,B 文献完全没有。当事人信息客观存在(在 A 文献上),并非源头无数据。

### 3.2 一条 CN-B 记录的实际字段清单(确认是「缺 key」而非「空值」)

```bash
curl -s 'https://search.houdutech.cn:9243/epo_docdb_v2_20260615/_doc/<CN-111354924-B 的 _id>'
# 或用 query 取 _source
```

该记录 `_source` 含:`publication_number / application_number / country / patent_type / doc_id /
family_id / priority_* / application_date / publication_date / patent_name / title_zh /
abstract / abstract_zh / cpc_code / cpc_main / class_cpc_*`。
**不含** `applicant`、`inventor`(也不含 `applicant_split`/`inventor_split`)。

### 3.3 全库覆盖率统计(`exists` 聚合,`track_total_hits:true`)

```bash
curl -s 'https://search.houdutech.cn:9243/epo_docdb_v2_20260615/_search' \
  -H 'Content-Type: application/json' -d '{
    "size": 0, "track_total_hits": true,
    "query": {"query_string": {"query": "country:CN AND patent_type:B"}},
    "aggs": {"has_applicant": {"filter": {"exists": {"field": "applicant"}}}}
  }'
```

把 query 换成下表各行即得:

| 子集 | 总量 | 有 `applicant` | 覆盖率 | 有 `inventor` | 覆盖率 |
|---|---|---|---|---|---|
| CN `patent_type=A`(申请) | 22,129,263 | 21,711,247 | **98.1%** | 21,522,175 | **97.3%** |
| **CN `patent_type=B`(授权)** | 8,358,159 | 1,089,309 | **13.0%** | 1,088,433 | **13.0%** |
| US `patent_type=A1` | 8,532,127 | 8,515,573 | 99.8% | — | — |
| US `patent_type=B2`(授权) | 5,527,645 | 5,472,291 | **99.0%** | — | — |

## 4. 根因分析

1. **不是空值/字段名映射问题**:`_source` 里 CN-B 记录直接没有 `applicant`/`inventor` key(`applicant_split`/`inventor_split` 也没有)。
2. **不是「授权文献通则」**:**US 授权(B2)当事人覆盖 99.0%**,正常。问题只集中在 **CN × B** 这一组合(13.0%)。
3. **当事人数据是存在的**:同一专利族的 CN-A 文献带齐当事人(§3.1)。
4. **倾向判断**:入库/DOCDB 解析管线在处理**中国授权(B)文献**时未抽取/未回填当事人(applicant/inventor)。可在族级别从 A 文献富集却未做。

> 备注:如确属 DOCDB 源端对 CN-B 不提供当事人,也建议在入库阶段用 `family_id` 从同族 A 文献回填,以保证中国专利当事人的可用性。

## 5. 期望行为

CN 授权(B)文献的 `applicant`/`inventor` 覆盖率应与 CN 申请(A)、US 文献相当(≈98%),或在缺失时由同族 A 文献回填。

## 6. 建议修复(任一)

1. **重抽**:修正 CN-B 文献的当事人解析,直接补 `applicant`/`inventor`。
2. **族级富集**:入库时若 B 文献当事人为空,按 `family_id` 取同族 A 文献的 `applicant`/`inventor` 回填(本报告 §3.1 已证可行)。
3. 至少在文档/字段说明中标注该已知缺口,便于下游做兜底。

## 7. 附:可能相关的次要问题(非本 bug 阻塞项)

- **`ipc_main` 全库稀疏**:CN-A 仅 3.1%、CN-B 仅 0.2% 有 `ipc_main`(`ipc_code`/`ipc_main_code` 同样稀疏);分类号实际落在 `cpc_main`(CN-A 77.8% / CN-B 80.3%)。若期望 IPC 可检索,需单独补 IPC 抽取;否则建议在字段文档里注明「分类以 CPC 为准,IPC 多数为空」。
- mapping 中存在 `applicant_split`/`inventor_split`/`class_ipc_*` 等字段,CN-B 记录同样未填充。
