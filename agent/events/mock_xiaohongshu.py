"""
agent/events/mock_xiaohongshu.py
Mock Xiaohongshu posts simulating what a real scraper would return.
20 varied posts with metadata for keyword pre-filtering.
"""

from datetime import datetime, timezone

SCRAPED_AT = datetime.now(timezone.utc).isoformat()


def _post(
    post_id: str,
    poster: str,
    raw_text: str,
    posted_date: str,
    *,
    keywords: list[str],
    faculty_tags: list[str] | None = None,
    year_tags: list[str] | None = None,
    is_noise: bool = False,
) -> dict:
    return {
        "id": post_id,
        "source": "xiaohongshu",
        "source_url": f"https://www.xiaohongshu.com/explore/{post_id}",
        "raw_text": raw_text.strip(),
        "poster": poster,
        "posted_date": posted_date,
        "scraped_at": SCRAPED_AT,
        "keywords": keywords,
        "faculty_tags": faculty_tags or ["all"],
        "year_tags": year_tags or ["all"],
        "is_noise": is_noise,
    }


MOCK_XHS_POSTS = [
    _post(
        "xhs_001", "大湾区青年文化交流协会",
        """
        【大湾区文化交流营2026】深圳·广州·香港三城联动，港澳台及内地在港大学生。
        7月15日-8月5日，参观美术馆、戏剧工作坊。名额30人，免费。
        申请截止：2026年6月20日。#大湾区 #文化交流 #人文艺术
        """,
        "2026-06-01",
        keywords=["cultural exchange", "arts", "humanities", "community", "大湾区"],
        faculty_tags=["arts", "all"],
    ),
    _post(
        "xhs_002", "深港科技创新联盟",
        """
        【深港澳AI创新大赛2026】智慧城市、医疗AI、金融科技、可持续发展。
        粤港澳在读大学生，2-4人组队。初赛截止7月1日，决赛深圳8月。
        奖金池RMB 500,000。#AI比赛 #人工智能 #hackathon
        """,
        "2026-06-02",
        keywords=["ai", "machine learning", "hackathon", "innovation", "competition"],
        faculty_tags=["engineering", "science", "all"],
    ),
    _post(
        "xhs_003", "港大文学社",
        """
        【两岸四地大学生中文文学奖2026】短篇小说、现代诗歌、散文组别。
        一等奖HK$8,000+出版机会。截止7月31日。
        内地在港学生可参加。#中文文学奖 #创意写作 #文学
        """,
        "2026-06-02",
        keywords=["writing", "literature", "creative writing", "arts", "中文"],
        faculty_tags=["arts", "all"],
    ),
    _post(
        "xhs_004", "港漂美食博主",
        """
        香港好吃的火锅店推荐🍲 海底捞、小蛮椒，人均HK$200-300。
        周末记得提前订位！#香港美食 #火锅推荐
        """,
        "2026-06-04",
        keywords=["food", "restaurant", "火锅"],
        is_noise=True,
    ),
    _post(
        "xhs_005", "港大心理学会",
        """
        【心理健康青年领袖计划2026】学习同伴支持、压力管理、校园心理健康倡导。
        面向港校本科及研究生，8-week培训，7月开课。报名截止6月25日。
        #心理健康 #peer support #wellbeing
        """,
        "2026-06-03",
        keywords=["psychology", "mental health", "wellbeing", "leadership", "workshop"],
        faculty_tags=["social sciences", "all"],
    ),
    _post(
        "xhs_006", "粤港澳青年舞蹈节",
        """
        【2026粤港澳大学生现代舞创作营】5天集训+汇报演出。
        有舞蹈基础者优先，也欢迎编舞/音乐设计背景同学跨学科参与。
        报名截止6月22日，营地7月12-17日。#舞蹈 #现代舞 #艺术
        """,
        "2026-06-04",
        keywords=["dance", "performing arts", "creative", "arts", "workshop"],
        faculty_tags=["arts", "all"],
    ),
    _post(
        "xhs_007", "深圳创客空间",
        """
        【Makerthon硬件创客马拉松】48小时做出可演示原型。
        欢迎工程、设计、计算机同学组队。提供3D打印和电子元件支持。
        报名截止6月28日，活动8月2-4日。#创客 #hardware #robotics
        """,
        "2026-06-05",
        keywords=["robotics", "hardware", "engineering", "hackathon", "innovation"],
        faculty_tags=["engineering", "architecture"],
    ),
    _post(
        "xhs_008", "港大环保学会",
        """
        【校园减废创新挑战2026】设计减少校园塑料/食物浪费的方案。
        所有港校学生可参加，个人或2-3人队。方案截止7月8日。
        优胜队伍获种子基金HK$10,000。#环保 #sustainability #创新
        """,
        "2026-06-05",
        keywords=["sustainability", "environment", "innovation", "community", "volunteering"],
        faculty_tags=["all", "science", "engineering"],
    ),
    _post(
        "xhs_009", "香港电影节协会",
        """
        【青年短片创作计划2026】提供拍摄指导和后期工作坊，完成5-10分钟短片。
        影视、传媒、艺术相关专业优先，也欢迎跨学科故事创作者。
        申请截止7月15日。#film #media #creative
        """,
        "2026-06-06",
        keywords=["film", "media", "creative", "video", "arts"],
        faculty_tags=["arts", "all"],
    ),
    _post(
        "xhs_010", "港大职业发展中心",
        """
        【暑期实习配对周2026】金融、咨询、科技、公共部门实习岗位说明会。
        面向大二至研一同学，6月18-20日连续三场线上+线下。
        免费登记。#实习 #career fair #求职
        """,
        "2026-06-06",
        keywords=["internship", "career", "recruitment", "finance", "consulting"],
        faculty_tags=["all", "business"],
        year_tags=["2", "3", "4", "master"],
    ),
    _post(
        "xhs_011", "岭南大学中文系",
        """
        【岭南文化考察团2026】走访广州永庆坊、佛山非遗工坊。
        中文、历史、文化研究背景同学优先，限额25人。截止6月19日。
        #岭南文化 #历史 #考察
        """,
        "2026-06-07",
        keywords=["history", "culture", "humanities", "research", "arts"],
        faculty_tags=["arts", "social sciences"],
    ),
    _post(
        "xhs_012", "港大Quant社团",
        """
        【量化交易校园赛2026】用公开数据完成策略回测任务，线上进行。
        金融、数学、统计、计算机同学都适合。报名截止7月3日。
        奖品：Bloomberg终端培训名额。#quant #finance #data analysis
        """,
        "2026-06-07",
        keywords=["finance", "statistics", "data analysis", "economics", "competition"],
        faculty_tags=["business", "economics", "science", "engineering"],
    ),
    _post(
        "xhs_013", "香港社工协会",
        """
        【社区服务学习营2026】与本地NGO合作探访长者、新来港家庭。
        社工、教育、社会科学同学优先，也欢迎任何有服务热忱的同学。
        7月6-13日，报名截止6月21日。#社工 #volunteering #community
        """,
        "2026-06-05",
        keywords=["social work", "volunteering", "community", "leadership"],
        faculty_tags=["social sciences", "education", "all"],
    ),
    _post(
        "xhs_014", "港大语言交换",
        """
        【英语-普通话-粤语语言伙伴计划】每周两次对练，匹配同专业伙伴。
        适合想提升口语的本地及非本地同学。6月30日开始，持续8周。
        #语言交换 #language #communication
        """,
        "2026-06-08",
        keywords=["language", "communication", "cultural exchange"],
        faculty_tags=["all"],
    ),
    _post(
        "xhs_015", "香港游戏开发者协会",
        """
        【Indie Game Jam HK 2026】72小时开发一款可玩demo，主题“城市故事”。
        程序、美术、叙事、音效同学均可组队。7月18-21日，报名截止7月1日。
        #game development #software engineering #creative
        """,
        "2026-06-08",
        keywords=["game development", "software engineering", "creative", "hackathon", "design"],
        faculty_tags=["engineering", "arts", "all"],
    ),
    _post(
        "xhs_016", "港大医学院科普团",
        """
        【医学科普创作大赛2026】制作短视频或图文科普作品。
        医学、生物、公共卫生、传媒学生均可。提交截止7月20日。
        #medicine #health #science communication
        """,
        "2026-06-09",
        keywords=["medicine", "health", "science", "communication", "research"],
        faculty_tags=["medicine", "science", "arts"],
    ),
    _post(
        "xhs_017", "香港摄影学会",
        """
        【城市光影摄影比赛2026】主题“香港的日与夜”，学生组免费参赛。
        接受手机或相机作品，截止8月10日。展览于9月举行。
        #photography #visual arts #creative
        """,
        "2026-06-09",
        keywords=["photography", "visual arts", "creative", "arts"],
        faculty_tags=["arts", "all"],
    ),
    _post(
        "xhs_018", "港大山学会",
        """
        【周末行山导赏队2026】龙脊、麦理浩径分段行，认识本地生态。
        公开招募队员，需基本体能。7月每周末，WhatsApp群报名。
        #hiking #outdoors #wellbeing
        """,
        "2026-06-10",
        keywords=["hiking", "outdoors", "wellbeing", "community"],
        faculty_tags=["all"],
    ),
    _post(
        "xhs_019", "小红书购物推荐",
        """
        618香港代购清单💄 美妆护肤打折合集，附购买链接和折扣码。
        #代购 #购物 #美妆 #促销
        """,
        "2026-06-10",
        keywords=["promo", "sale", "shopping", "beauty"],
        is_noise=True,
    ),
    _post(
        "xhs_020", "港大公共政策论坛",
        """
        【青年公共政策案例大赛2026】分析香港房屋、交通或人口政策并提出建议。
        政治学、公共行政、法律、社会科学同学尤其适合。方案截止7月12日。
        #公共政策 #policy #research #debate
        """,
        "2026-06-10",
        keywords=["policy", "law", "research", "debate", "social sciences"],
        faculty_tags=["social sciences", "law", "all"],
    ),
]


def get_mock_xhs_posts() -> list:
    return MOCK_XHS_POSTS
