"""
agent/events/mock_xiaohongshu.py
Mock Xiaohongshu posts simulating what a real scraper would return.
Flat list — no persona sorting. The event extractor handles relevance matching.
"""

from datetime import datetime, timezone
SCRAPED_AT = datetime.now(timezone.utc).isoformat()

MOCK_XHS_POSTS = [
    {
        "id": "xhs_001",
        "source": "xiaohongshu",
        "source_url": "https://www.xiaohongshu.com/explore/xhs_001",
        "raw_text": """
【大湾区文化交流营2026】招募令来啦！✨

深圳·广州·香港三城联动文化体验营，专为港澳台及内地在港就读大学生设计！

📅 时间：2026年7月15日 - 8月5日（共3周）
📍 地点：深圳、广州、香港西九龙文化区

活动亮点：
🎨 参观深圳设计互联、广东美术馆
✍️ 与粤港澳知名作家、艺术家交流
🎭 参与大湾区原创戏剧工作坊

名额：30人（港澳台学生优先）
费用：全程免费
申请截止：2026年6月20日
联系：gbaculture2026@gmail.com

#大湾区 #文化交流 #香港大学 #内地生 #人文艺术
        """,
        "poster": "大湾区青年文化交流协会",
        "posted_date": "2026-06-01",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "xhs_002",
        "source": "xiaohongshu",
        "source_url": "https://www.xiaohongshu.com/explore/xhs_002",
        "raw_text": """
【深港澳AI创新大赛2026】来啦！🤖

面向粤港澳大湾区在读大学生的人工智能创新比赛！

赛题方向：智慧城市、医疗AI、金融科技、可持续发展

报名资格：全日制在读本科生或研究生（香港院校均可）
队伍规模：2-4人
初赛截止：2026年7月1日
决赛地点：深圳科技园（2026年8月）

奖金池：总计RMB 500,000
一等奖：RMB 100,000

报名：szhai-competition.com

港大工程、计算机的同学快来组队啊！🏆

#AI比赛 #深港澳 #大学生竞赛 #人工智能
        """,
        "poster": "深港科技创新联盟",
        "posted_date": "2026-06-02",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "xhs_003",
        "source": "xiaohongshu",
        "source_url": "https://www.xiaohongshu.com/explore/xhs_003",
        "raw_text": """
【两岸四地大学生中文文学奖2026】开始报名！📝

主办：香港中文文学发展协会
对象：两岸四地在读大学生
组别：短篇小说、现代诗歌、散文

奖项：
🥇 一等奖：HK$8,000 + 出版机会
🥈 二等奖：HK$4,000

截止日期：2026年7月31日
投稿：literaryaward2026@hkcla.org

内地在港学生完全可以参加！🎉

#中文文学奖 #创意写作 #香港 #大学生比赛
        """,
        "poster": "港大文学社",
        "posted_date": "2026-06-02",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "xhs_004",
        "source": "xiaohongshu",
        "source_url": "https://www.xiaohongshu.com/explore/xhs_004",
        "raw_text": """
【奖学金】粤港澳大湾区青年发展奖学金2026开放申请！💰

申请资格：
✅ 在香港、澳门或广东省高校就读的全日制本科生
✅ 成绩良好（无具体GPA要求，以个人陈述为主）
✅ 有志于推动大湾区文化、教育或社会发展

奖励：每人RMB 20,000（约HK$22,000）

材料：个人陈述 + 两封推荐信 + 成绩单
截止：2026年7月15日
申请：gba-youth.org.hk/scholarship

人文、社科、艺术类学生特别欢迎！不需要高GPA 🎉

#大湾区奖学金 #港大 #内地生
        """,
        "poster": "大湾区青年发展基金",
        "posted_date": "2026-06-01",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "xhs_005",
        "source": "xiaohongshu",
        "source_url": "https://www.xiaohongshu.com/explore/xhs_005",
        "raw_text": """
【干货】2026香港金融行业秋招时间线整理📊

投行类：
- Goldman Sachs: 申请截止 2026年7月15日
- Morgan Stanley: 申请截止 2026年7月31日
- JPMorgan: 申请截止 2026年8月1日

本地银行：
- HSBC Graduate Programme: 申请截止 2026年7月31日
- Bank of China HK: 申请截止 2026年8月15日

四大会计师事务所：Big4均在2026年7-9月开放申请

✅ 港大BBA背景在本地金融圈认可度很高

#香港金融 #秋招 #应届毕业生 #港大BBA
        """,
        "poster": "香港金融求职攻略",
        "posted_date": "2026-06-03",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "xhs_006",
        "source": "xiaohongshu",
        "source_url": "https://www.xiaohongshu.com/explore/xhs_006",
        "raw_text": """
香港好吃的火锅店推荐🍲

最近发现几家超好吃的火锅！
1. 海底捞 铜锣湾店 ⭐⭐⭐⭐⭐
2. 小蛮椒 旺角店 ⭐⭐⭐⭐
人均HK$200-300，周末记得提前订位！

#香港美食 #火锅推荐 #港漂生活
        """,
        "poster": "港漂美食博主",
        "posted_date": "2026-06-04",
        "scraped_at": SCRAPED_AT
    }
]


def get_mock_xhs_posts() -> list:
    return MOCK_XHS_POSTS
