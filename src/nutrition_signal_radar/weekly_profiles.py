"""Structured expert-track writing profiles for publishable weekly articles.

The "expert track" idea is that a domain is not a label but a complete writing
path: topic positioning, audience, content boundary, structure, expression
style, quality bar and compliance are bundled into one reusable profile and
injected into the generation prompt, the quality gate and the generation
record.  Multiple template versions can coexist under the same track for
different audiences or depths.

The style rules are style only.  They may make the writing read like a domain
columnist, but the quality-gate revision loop is forbidden from touching
facts, data or sources — every factual claim must still come from the
retrieved public pages.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeeklyArticleProfile:
    key: str
    label: str
    audience: str
    positioning: str
    boundary: tuple[str, ...]
    structure: tuple[str, ...]
    style: tuple[str, ...]
    forbidden_style_words: tuple[str, ...]
    quality: tuple[str, ...]
    compliance: tuple[str, ...]
    word_count_min: int
    word_count_max: int
    exemplar: str = ""


DECISION_MAKER_PROFILE = WeeklyArticleProfile(
    key="industry_lead",
    label="营养健康行业主笔 · 决策者版",
    audience="营养健康行业品牌方、原料商、渠道及企业决策者",
    positioning=(
        "行业判断型内容：帮决策者看清动向、影响与下一步，"
        "不是消费者健康科普、资讯搬运或百科综述。"
    ),
    boundary=(
        "只写监测时间段内发生的新事件；没有新进展的信号直接省略，"
        "不得用更早的监管、市场、科研或企业事件填充",
        "无法用公开来源支撑的细节一律不写进正文，只在 editor_notes 说明；"
        "绝不编造数据或 URL",
    ),
    structure=(
        "标题严格采用「营养保健行业周报|钩子式主标题」：竖线后是一条完整、"
        "具体的钩子式主标题（推荐疑问句或反差式陈述），聚焦本周最值得决策者"
        "关注的一件事，不要用顿号罗列主题词",
        "开头一段 80—150 字导语，概括最重要的动向与判断",
        "正文按主题分节（如：一、监管动态；二、市场与数据；三、企业动作；"
        "四、科研与国际动态），每个分节不少于 2 条",
        "每条内容以“日期 + 主体 + 事件 + 关键数据 + 影响/判断”呈现",
    ),
    style=(
        "像垂直领域主笔一样写作：直接、有判断、有取舍，不要平铺直叙的资料汇编",
        "句式长短交错，段落长短不一；少用排比式三连列举",
        "避免 AI 高频套话与连接词，如：首先、其次、总而言之、综上所述、"
        "值得注意的是、不难发现、众所周知、与此同时等",
        "少用“赋能”“抓手”“闭环”等空话，多用具体动作和具体数字",
        "每条事实写成确定性行业表述；指代时间用具体日期（如“7月17日”）"
        "或自然时段（如“最近两周”）",
        "可以有主笔的判断语气（如“更值得关注的是”），但判断必须建立在"
        "前面已陈述的事实上，不允许凭空猜测或虚构态度",
    ),
    forbidden_style_words=(
        "首先",
        "其次",
        "总而言之",
        "综上所述",
        "值得注意的是",
        "不难发现",
        "众所周知",
        "与此同时",
        "赋能",
        "抓手",
        "闭环",
    ),
    quality=(
        "正文 1800—2800 字（按中文字符计数）",
        "正文至少 2 个二级分节，每个分节有具体内容",
        "正文至少出现一处具体日期（如“7月17日”）",
        "标题与正文不得出现“待补证”“本期”等内部标记或内部口径词",
        "关键数据尽量给出具体数值，并能在来源清单中找到对应页面",
    ),
    compliance=(
        "每条重要事实必须落到公开页面 URL，来源写入 sources 字段",
        "不触发任何微信公众号写入或发布动作；发布由人工复制完成",
    ),
    word_count_min=1800,
    word_count_max=2800,
    exemplar=(
        "参考结构示例（仅示范文体结构，内容须替换为真实信号）：\n"
        "## 一、监管动态\n\n"
        "7月17日，市场监管总局就保健食品功能目录征求意见，"
        "骨关节健康类功能首次进入讨论范围。对企业而言，"
        "这既是新功能申报的窗口，也意味着现有宣称口径需要逐条重审。\n\n"
        "7月22日，总局部署网络食品虚假宣传专项整治，直播话术被明确列为检查重点。"
        "结合近期处罚案例，宣称合规的边界已从详情页延伸到直播与私域。"
    ),
)

DEEP_READER_PROFILE = WeeklyArticleProfile(
    key="deep_analysis",
    label="营养健康行业主笔 · 深度解读版",
    audience="营养健康行业品牌方、原料商、渠道及企业决策者",
    positioning=(
        "面向需要更完整判断依据的决策者的深度解读：允许更多背景与推演，"
        "但每条结论仍须落到公开来源。"
    ),
    boundary=(
        "以监测时间段内的事件为主线，允许回溯必要的监管与科研背景，"
        "但背景信息同样必须可溯源",
        "无法用公开来源支撑的细节一律不写进正文，只在 editor_notes 说明；"
        "绝不编造数据或 URL",
    ),
    structure=(
        "标题严格采用「营养保健行业周报|钩子式主标题」：竖线后是一条完整、"
        "具体的钩子式主标题（推荐疑问句或反差式陈述），聚焦本周最值得决策者"
        "关注的一件事，不要用顿号罗列主题词",
        "开头一段导语，概括判断结论",
        "正文按主题分节，每个分节不少于 2 条，节末可加一段“值得注意的判断”推演",
        "每条内容以“日期 + 主体 + 事件 + 关键数据 + 影响/判断”呈现",
    ),
    style=(
        "像垂直领域主笔一样写作：直接、有判断、有取舍，不要平铺直叙的资料汇编",
        "句式长短交错，段落长短不一；少用排比式三连列举",
        "避免 AI 高频套话与连接词，如：首先、其次、总而言之、综上所述、"
        "值得注意的是、不难发现、众所周知、与此同时等",
        "少用“赋能”“抓手”“闭环”等空话，多用具体动作和具体数字",
        "每条事实写成确定性行业表述；指代时间用具体日期或自然时段",
        "推演段可以提出判断，但必须与来源事实分开表述，不得把推演写成已确认事实",
    ),
    forbidden_style_words=DECISION_MAKER_PROFILE.forbidden_style_words,
    quality=(
        "正文 2600—3600 字（按中文字符计数）",
        "正文至少 2 个二级分节，每个分节有具体内容",
        "正文至少出现一处具体日期（如“7月17日”）",
        "标题与正文不得出现“待补证”“本期”等内部标记或内部口径词",
        "关键数据尽量给出具体数值，并能在来源清单中找到对应页面",
    ),
    compliance=DECISION_MAKER_PROFILE.compliance,
    word_count_min=2600,
    word_count_max=3600,
)

WEEKLY_ARTICLE_PROFILES = {
    DECISION_MAKER_PROFILE.key: DECISION_MAKER_PROFILE,
    DEEP_READER_PROFILE.key: DEEP_READER_PROFILE,
}
DEFAULT_WEEKLY_ARTICLE_PROFILE = DECISION_MAKER_PROFILE


def resolve_weekly_article_profile(profile_key: str = "") -> WeeklyArticleProfile:
    """Return a known profile, falling back to the default for empty/unknown keys."""

    if not profile_key:
        return DEFAULT_WEEKLY_ARTICLE_PROFILE
    return WEEKLY_ARTICLE_PROFILES.get(
        profile_key, DEFAULT_WEEKLY_ARTICLE_PROFILE
    )
