# Agent 场景对比报告 (n=8, k=8)

场景: 技术尽调/R&D 情报 agent。主分来自 agent 基于搜索结果写出的最终回答。

## 总览
| Metric | full_agent | baidu_only |
|--------|------------|------------|
| avg_latency_ms | 5074 | 1298 |
| p95_latency_ms | 6624 | 1396 |
| avg_web_results | 8.0 | 7.2 |
| avg_academic_results | 7.0 | 0.0 |
| avg_patent_results | 8.0 | 0.0 |
| answer_wins | 8 | 0 |
| answer_ties | 0 | 0 |
| avg_answer_score | 7.75 | 4.75 |
| answer_audit_flags | 2 | 2 |

## 场景明细
| ID | Domain | Evidence full(web/acad/pat) | Evidence baidu | Answer winner | Answer scores | Reason |
|----|--------|-----------------------------|----------------|---------------|---------------|--------|
| sodium_battery_cathode | battery | 8/8/8 | 6 | full_agent | 8:5 | full_agent完整交付专利布局、学术进展、产业信息三维整合分析,8张专利表、大连化物所研究细节、具体成本时间表;baidu仅产业信息完整但主动坦白学术/专利缺失,无对标性建议。full_agent有2条unsupported_refs |
| solid_state_sulfide_electrolyte | battery | 8/8/8 | 8 | full_agent | 8:5 | full_agent检索多源(学术、专利、产业),获8条web+8条学术+8条专利,诚实披露学术论文缺失,并系统阐述专利布局(宁德/天赐/浙江等8家申请人)、量产瓶颈、风险机会。baidu_only仅web+百科,missing_acade |
| perovskite_encapsulation | solar | 8/8/8 | 4 | full_agent | 8:4 | full_agent完整回答了学术研究、专利申请人、商业挑战和投资方向四大要求，并有学术论文和专利证据支撑。baidu_only仅返回Web结果（无学术/专利检索），承认缺口且答案不完整，无法支撑专业尽调。 |
| mrna_lnp_delivery | biotech | 8/8/8 | 8 | full_agent | 8:4 | full_agent提供学术论文证据、8项专利详情、产业应用分析和明确的机会/风险判断。baidu_only仅web源、无学术/专利数据、存在关键信息缺口。full_agent虽专利部分需补充验证,但整体完整度与可用性显著优于baidu_o |
| crispr_base_editing | biotech | 8/8/8 | 8 | full_agent | 7:5 | Full agent整合多源数据(学术+网页+专利),提供8项学术论文、CBE/ABE/PE技术分类、FDA批准动态、Cas9变体进展、递送系统。但专利检索失效,承认证据缺口。Baidu仅web数据,无学术库,无专利数据,技术壁垒分析不完整 |
| foldable_hinge | electronics | 8/8/8 | 8 | full_agent | 7:5 | Full_agent检索了学术库和专利库,虽未获得直接专利结果但至少尝试了;诚实说明论文稀缺性。Baidu仅用网络文献,完全缺失学术和专利维度,信息来源单一。两者都未核获具体专利号/申请人细节,但full_agent的综合分析更完整。 |
| autonomous_lidar_fusion | autonomous_driving | 8/8/8 | 8 | full_agent | 8:5 | full_agent提供8份学术论文、8份专利且涵盖安全威胁等前沿研究;baidu仅8条web结果,缺学术论文和专利证据。full_agent完整覆盖任务要求,baidu明确声称缺失学术和专利数据,可靠性明显下降。 |
| virtual_anchor_ecommerce | ai_product | 8/0/8 | 8 | full_agent | 8:5 | full_agent检索8个专利+8个web资源,完整梳理申请人与技术方向;baidu仅8个web无专利,缺失核心知识产权布局。两者学术论文检索均为零,但full_agent诚实标注gap;baidu虚假承诺学术证据。full_agent市 |

## 回答质量维度
| Dimension | full_agent | baidu_only |
|-----------|------------|------------|
| grounding | 1.75 / 2 | 0.88 / 2 |
| research | 1.50 / 2 | 0.25 / 2 |
| patent | 1.50 / 2 | 0.00 / 2 |
| synthesis | 2.00 / 2 | 1.00 / 2 |
