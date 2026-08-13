"""Deterministic corpus for the embedding intent-routing classifier.

The splits are intentionally defined in source instead of being synthesized by
an LLM at training time.  This makes classifier artifacts reproducible and
keeps validation/holdout labels stable across runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable, Sequence


SOURCE_ORDER = ("academic", "patent", "legal")
GROUP_SOURCES: dict[str, tuple[str, ...]] = {
    "general": (),
    "academic": ("academic",),
    "patent": ("patent",),
    "legal": ("legal",),
    "academic+patent": ("academic", "patent"),
    "academic+legal": ("academic", "legal"),
    "patent+legal": ("patent", "legal"),
    "academic+patent+legal": SOURCE_ORDER,
}


@dataclass(frozen=True)
class IntentRouteCase:
    case_id: str
    split: str
    group: str
    query: str
    source_types: tuple[str, ...]


def _explicit(
    split: str,
    group: str,
    queries: Sequence[str],
) -> list[IntentRouteCase]:
    prefix = group.replace("+", "_")
    return [
        IntentRouteCase(
            case_id=f"{split}-{prefix}-{index:03d}",
            split=split,
            group=group,
            query=query,
            source_types=GROUP_SOURCES[group],
        )
        for index, query in enumerate(queries, 1)
    ]


def _templated(
    split: str,
    group: str,
    topics: Sequence[str],
    templates: Sequence[str],
) -> list[IntentRouteCase]:
    queries = [template.format(topic=topic) for topic, template in product(
        topics, templates
    )]
    return _explicit(split, group, queries)


_TRAIN_GENERAL_HARD_NEGATIVES = (
    "苹果公司的法定名称和注册地址是什么",
    "What is the legal name of ByteDance in Singapore?",
    "网站页脚的 legal notice 在哪里",
    "legal size paper 和 A4 纸尺寸有什么区别",
    "推荐几部好看的律政剧",
    "Law & Order 最新一季演员表",
    "法律出版社今年有哪些招聘岗位",
    "法学院官网的硕士招生简章",
    "司法考试报名入口和考试时间",
    "律师事务所官网电话和办公地址",
    "合同管理软件有哪些好用的产品",
    "公司声明称将依法追究责任，这条新闻是真的吗",
    "电视剧《底线》的剧情介绍",
    "电影里陪审团制度是怎么演的",
    "专利皮鞋应该怎样清洁",
    "patent leather shoes care tips",
    "电话是谁发明的，讲讲它的历史",
    "爱迪生有哪些著名发明",
    "专利代理师考试什么时候出成绩",
    "知识产权局办公地址和客服电话",
    "某公司宣传拥有上千项专利的新闻报道",
    "如何在简历里描述自己的发明项目",
    "invention convention tickets and venue",
    "专利检索网站的账号登录不了怎么处理",
    "论文答辩 PPT 应该用什么模板",
    "毕业论文格式怎么调整页码",
    "paper tiger 这个短语是什么意思",
    "打印机卡纸怎么修",
    "academic calendar for Stanford University",
    "学术会议酒店预订和交通攻略",
    "期刊编辑部的投稿系统打不开",
    "DOI 是什么缩写",
    "引用格式 APA 和 MLA 有什么区别",
    "研究生如何安排每天的学习时间",
    "帮我润色这段论文摘要，不需要查资料",
    "生成一份实验室周会纪要模板",
    "介绍一下《民法典》这本电视剧道具书的封面设计",
    "《专利法》教材在哪里买价格便宜",
    "知识产权专业大学排名和就业方向",
    "法院附近有什么餐厅",
    "最高人民法院博物馆开放时间",
    "arXiv 网站今天是不是宕机了",
    "USPTO 官网密码忘记了如何重置",
    "WIPO 总部在哪个城市",
    "查一下今天关于人工智能监管的新闻",
    "介绍一家新能源公司的产品与融资历史",
    "固态电池概念股今天为什么上涨",
    "北京今天的天气和空气质量",
    "translate the phrase legal counsel into Chinese",
    "legal affairs department email address for this company",
    "courthouse parking and public transit directions",
    "patent leather jacket repair service near me",
    "专利数据库页面一直显示验证码错误",
    "发明家主题展览的门票多少钱",
    "论文查重网站怎么修改绑定手机号",
    "期刊投稿系统的客服邮箱是什么",
    "法院题材综艺节目嘉宾名单",
    "学术出版社编辑岗位招聘要求",
    "帮我解释 citation 这个英文单词",
    "今天有哪些医药专利交易相关新闻",
    "法院公交站周边哪里可以停车",
    "去最高法院参观应该在哪个地铁口下车",
    "论文查重平台忘记密码怎么找回",
    "学校论文管理系统无法上传附件",
)

_VALIDATION_GENERAL_HARD_NEGATIVES = (
    "legal department 的中文怎么翻译",
    "这家公司的 registered legal name 是什么",
    "推荐一部讲律师生活的美剧",
    "法考培训班哪家口碑好",
    "法院地铁站附近停车方便吗",
    "如何清理 patent leather handbag",
    "蒸汽机的发明故事",
    "专利代理机构前台联系电话",
    "论文查重系统登录页面打不开",
    "给毕业论文封面推荐一种字体",
    "peer review 这个词是什么意思",
    "academic year 和 calendar year 的区别",
    "国家知识产权局今天发布了什么新闻",
    "某明星起诉公司的娱乐新闻进展",
    "介绍 OpenAlex 这个网站",
    "帮我写一段不引用资料的公司简介",
    "合同电子签章产品价格对比",
    "附近哪里可以买到法律出版社的书",
)


_TRAIN_TOPICS = {
    "academic": (
        "固态电池界面稳定性", "大语言模型幻觉检测", "阿尔茨海默病早筛",
        "城市热岛效应", "钙钛矿太阳能电池", "联邦学习隐私保护",
        "青少年睡眠质量", "量子纠错码",
    ),
    "patent": (
        "固态电池电解质", "可折叠屏铰链", "无人机避障", "mRNA 递送",
        "图像超分辨率", "快充温控", "农业采摘机器人", "语音唤醒",
    ),
    "legal": (
        "个人信息跨境传输", "劳动合同试用期", "网络平台算法推荐",
        "未成年人网络保护", "生成式人工智能服务", "商品房预售",
        "公司股东知情权", "食品安全惩罚性赔偿",
    ),
    "academic+patent": (
        "钠离子电池", "脑机接口", "碳捕集材料", "自动驾驶感知",
        "蛋白质结构预测", "工业缺陷检测",
    ),
    "academic+legal": (
        "平台用工劳动关系", "人脸识别隐私保护", "生成式 AI 著作权",
        "自动驾驶事故责任", "网络暴力治理", "数据跨境合规",
        "环境公益诉讼", "未成年人游戏防沉迷",
    ),
    "patent+legal": (
        "标准必要专利许可", "职务发明奖励", "药品专利链接",
        "专利侵权等同原则", "外观设计专利纠纷", "商业秘密与专利保护",
    ),
    "academic+patent+legal": (
        "生成式人工智能训练数据", "基因编辑治疗", "自动驾驶激光雷达",
        "数字疗法", "绿色氢能电解槽", "医疗影像辅助诊断",
    ),
}

_VALIDATION_TOPICS = {
    "academic": ("微塑料健康风险", "检索增强生成", "可降解塑料", "儿童近视干预"),
    "patent": ("钙钛矿封装", "仓储分拣机器人", "无线充电", "新型胰岛素泵"),
    "legal": ("直播带货消费者权益", "竞业限制补偿", "电子证据真实性", "反电信网络诈骗"),
    "academic+patent": ("柔性传感器", "低空飞行器电池", "靶向药物递送"),
    "academic+legal": ("深度伪造人格权", "外卖骑手权益", "气候变化诉讼", "科研数据开放"),
    "patent+legal": ("人工智能发明人资格", "生物医药专利强制许可", "芯片专利无效纠纷"),
    "academic+patent+legal": ("合成生物学", "具身智能机器人", "可穿戴医疗设备"),
}


_TRAIN_TEMPLATES = {
    "academic": (
        "检索{topic}的同行评审论文和综述",
        "Find journal articles, citations and recent studies about {topic}",
    ),
    "patent": (
        "查找{topic}相关专利、申请号和权利要求",
        "Search patent families, assignees and prior art for {topic}",
    ),
    "legal": (
        "查询{topic}适用的现行法律法规、法条和司法解释",
        "{topic}在中国法下如何规定，请给出法律依据",
        "Find the applicable Chinese statutes and regulations on {topic}",
        "核实{topic}相关规范性文件的效力与具体条款",
    ),
    "academic+patent": (
        "同时检索{topic}的学术论文和专利布局",
        "Compare peer-reviewed research with patent filings about {topic}",
        "调研{topic}的论文证据、专利族和主要申请人",
    ),
    "academic+legal": (
        "检索{topic}的法学论文，并核对现行法条和司法解释",
        "Research academic literature and applicable Chinese law on {topic}",
        "围绕{topic}做文献综述，同时查法规、判例依据",
        "找{topic}的同行评审研究以及对应法律规则",
        "Find legal scholarship, empirical studies, and binding authority on {topic}",
        "Review research evidence alongside statutes and court doctrine for {topic}",
        "Find scholarship plus binding Chinese legal authority about {topic}",
    ),
    "patent+legal": (
        "检索{topic}相关专利，并核对适用法条与司法解释",
        "Find patent claims and the governing Chinese legal rules for {topic}",
        "分析{topic}的专利族、权利要求和侵权裁判依据",
    ),
    "academic+patent+legal": (
        "全面调研{topic}：论文、专利和现行法律法规都要查",
        "Find academic studies, patent families, and applicable law on {topic}",
        "对{topic}同时做文献综述、专利布局和法律合规分析",
        "检索{topic}的论文证据、专利权利要求及司法规则",
        "Map scholarly evidence, invention claims, and governing law for {topic}",
        "查{topic}的研究文献、专利申请人，也核验监管和裁判依据",
    ),
}

_VALIDATION_TEMPLATES = {
    "academic": (
        "有哪些高质量研究讨论{topic}？请找论文",
        "Search scholarly publications and systematic reviews on {topic}",
    ),
    "patent": (
        "盘点{topic}技术的核心发明专利与申请人",
        "Retrieve publication numbers and claims for {topic} inventions",
    ),
    "legal": (
        "处理{topic}争议应援引哪些有效法条",
        "请查{topic}对应法规是否仍然有效",
        "Which Chinese legal provisions and judicial interpretations govern {topic}?",
    ),
    "academic+patent": (
        "查{topic}的研究进展，也看相关专利申请",
        "I need both papers and patent landscape evidence for {topic}",
    ),
    "academic+legal": (
        "查找{topic}的实证研究及法院适用的法律依据",
        "需要{topic}相关论文，同时核验法规和司法解释",
        "Find scholarship plus binding Chinese legal authority about {topic}",
    ),
    "patent+legal": (
        "调查{topic}的专利权利要求和法院裁判规则",
        "Search patents together with statutes governing {topic}",
    ),
    "academic+patent+legal": (
        "研究{topic}时请覆盖论文、发明专利与监管规定",
        "Build an evidence map of studies, patents, and law for {topic}",
        "{topic}有哪些学术证据、专利壁垒和法律限制",
    ),
}


def training_cases() -> tuple[IntentRouteCase, ...]:
    cases = _explicit("train", "general", _TRAIN_GENERAL_HARD_NEGATIVES)
    for group, topics in _TRAIN_TOPICS.items():
        cases.extend(_templated(
            "train", group, topics, _TRAIN_TEMPLATES[group]
        ))
    return tuple(cases)


def validation_cases() -> tuple[IntentRouteCase, ...]:
    cases = _explicit(
        "validation", "general", _VALIDATION_GENERAL_HARD_NEGATIVES
    )
    for group, topics in _VALIDATION_TOPICS.items():
        cases.extend(_templated(
            "validation", group, topics, _VALIDATION_TEMPLATES[group]
        ))
    return tuple(cases)


_HOLDOUT_QUERIES = {
    "general": (
        "今天量子计算领域有什么新闻",
        "legal tender 这个短语是什么意思",
        "某科技公司的法定英文名称是什么",
        "推荐三个可以下载论文封面模板的网站",
        "专利皮革和普通皮革有什么差别",
        "瓦特改良蒸汽机的历史故事",
        "知识产权专业毕业后好找工作吗",
        "法庭题材电影《十二怒汉》的剧情",
        "法院附近评分最高的咖啡店",
        "arXiv 注册邮件一直收不到怎么办",
        "专利检索平台会员一年多少钱",
        "如何给论文参考文献自动编号",
        "国家知识产权局领导今天出席了什么活动",
        "律师事务所实习生招聘信息",
        "what size is legal paper",
        "write a short company profile without doing research",
    ),
    "academic": (
        "找几篇关于多模态大模型评测的论文",
        "检索肿瘤免疫治疗耐药机制的系统综述",
        "有哪些同行评审研究分析平台算法偏见",
        "Find recent journal articles on sodium-ion battery recycling",
        "搜索 arXiv 上关于 agent memory 的文章",
        "关于专利质量评价有哪些计量研究论文",
        "查找法律大模型基准测试的学术文献",
        "给我 DOI 和引用量较高的睡眠研究",
    ),
    "patent": (
        "查询某公司的固态电池专利族",
        "检索人形机器人关节结构的发明专利",
        "找无线耳机降噪算法的申请号和权利要求",
        "Search USPTO filings for foldable display hinges",
        "谁申请了 CRISPR 递送系统相关专利",
        "盘点光伏逆变器的核心专利和申请人",
        "查一下这个技术方案有没有相似的在先专利",
        "Find WIPO publication numbers for smart insulin pens",
    ),
    "legal": (
        "民法典关于格式条款提示义务如何规定",
        "查询数据安全法中数据出境的具体条款",
        "竞业限制补偿金有没有现行司法解释",
        "What Chinese regulation governs recommendation algorithms?",
        "核实网络安全审查办法现在是否有效",
        "《专利法》第六十四条的现行内容是什么",
        "论文抄袭争议可以援引哪些著作权法条",
        "最高法关于劳动争议的新司法解释全文",
    ),
    "academic+patent": (
        "调研锂硫电池的论文进展和专利布局",
        "同时找脑机接口研究文献与核心专利族",
        "Compare papers and patent claims for perovskite tandem cells",
        "查柔性电子皮肤的学术文章、申请号和申请人",
        "蛋白质降解技术有哪些研究证据和发明专利",
        "做一份低空无人机避障的文献与专利综述",
    ),
    "academic+legal": (
        "找平台劳动者权益的实证论文和适用法条",
        "研究人脸识别进小区的问题，要论文也要司法解释",
        "Find scholarship and Chinese legal authority on deepfake liability",
        "检索数据确权的法学文献以及现行法规",
        "自动驾驶伦理有哪些研究，事故责任法律如何规定",
        "做网络暴力治理综述并核验法院裁判依据",
    ),
    "patent+legal": (
        "查标准必要专利的权利要求与许可法规",
        "检索药品专利并核对专利链接制度的法条",
        "Find relevant patents and infringement doctrine for AI chips",
        "分析职务发明专利和奖励报酬司法解释",
        "外观设计专利有哪些在先申请与侵权判例",
        "查商业秘密转专利的申请情况和法律风险",
    ),
    "academic+patent+legal": (
        "全面调查基因编辑：论文、专利族和监管法规",
        "对自动驾驶大模型做学术、专利及法律三方面检索",
        "Find studies, patent claims, and applicable law for digital therapeutics",
        "查脑机接口的研究证据、发明申请和伦理监管规定",
    ),
}


def holdout_cases() -> tuple[IntentRouteCase, ...]:
    cases: list[IntentRouteCase] = []
    for group, queries in _HOLDOUT_QUERIES.items():
        cases.extend(_explicit("holdout", group, queries))
    return tuple(cases)


def group_counts(cases: Iterable[IntentRouteCase]) -> dict[str, int]:
    counts = {group: 0 for group in GROUP_SOURCES}
    for case in cases:
        counts[case.group] += 1
    return {group: count for group, count in counts.items() if count}
