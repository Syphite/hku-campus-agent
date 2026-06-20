"""
agent/events/mock_linkedin.py
Mock LinkedIn post data simulating what a real LinkedIn scraper would return.
For the prototype demo — replace with real scraper post-hackathon.

Posts are realistic representations of competition/event announcements
that would appear on LinkedIn relevant to HKU students.
"""

from datetime import datetime, timezone

SCRAPED_AT = datetime.now(timezone.utc).isoformat()

MOCK_LINKEDIN_POSTS = [
    {
        "id": "li_001",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/microsoft-hk",
        "raw_text": """
        🚀 Microsoft Imagine Cup 2026 — Hong Kong Regional is NOW OPEN!
        
        We are looking for student innovators to build technology solutions 
        addressing real-world challenges using AI, Azure, and Microsoft tools.
        
        Open to all university students in Hong Kong.
        Team size: 1-4 members.
        Regional finals: August 2026 in Hong Kong.
        Grand prize: HK$50,000 + trip to global finals in Seattle.
        
        Application deadline: July 15, 2026
        Apply at: imaginecup.microsoft.com
        
        Skills valued: AI/ML, cloud computing, app development, problem solving.
        No specific major required — we welcome all disciplines!
        
        #ImaginneCup #Microsoft #StudentInnovation #HongKong
        """,
        "poster": "Microsoft Hong Kong",
        "posted_date": "2026-06-01",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_002",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/google-hk",
        "raw_text": """
        📢 Google Solution Challenge 2026 — Registration Open!
        
        Build a solution to one of the UN's 17 Sustainable Development Goals 
        using Google technologies.
        
        Who can apply: University students worldwide (team of 1-4)
        Technologies: Any Google product or platform
        Deadline: July 31, 2026
        Prizes: Top 3 global winners receive $4,000 USD each
        
        This is a great opportunity to build your portfolio and make an impact.
        Especially relevant for CS, Engineering, and Data Science students.
        
        Register: developers.google.com/community/gdsc/solution-challenge
        
        #GoogleSolutionChallenge #SDGs #StudentDeveloper
        """,
        "poster": "Google Developer Student Clubs HK",
        "posted_date": "2026-06-02",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_003",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/hkust-fintech",
        "raw_text": """
        💡 HKUST FinTech Competition 2026 — Open for Registration
        
        Are you passionate about the future of finance and technology?
        Join the largest student fintech competition in Hong Kong!
        
        Categories:
        - Blockchain & DeFi
        - AI in Financial Services  
        - RegTech & Compliance
        - Insurtech
        
        Open to undergraduate and postgraduate students from all HK universities.
        Team size: 2-5 members.
        Cash prizes totalling HK$200,000.
        
        Submission deadline: July 20, 2026
        Info session: June 20, 2026 at HKUST campus (2pm-4pm)
        
        Register: fintechcompetition.hkust.edu.hk
        
        #FinTech #HKUST #StudentCompetition #Finance #Blockchain
        """,
        "poster": "HKUST FinTech Institute",
        "posted_date": "2026-06-03",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_004",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/deloitte-hk",
        "raw_text": """
        🎯 Deloitte Technology Fast 500 Student Case Competition 2026
        
        Deloitte Hong Kong invites final year and postgraduate students to 
        tackle a real business transformation challenge for a leading HK company.
        
        Open to: Final year undergraduate and postgraduate students
        Focus: Digital transformation, AI strategy, business consulting
        Team size: 3-4 members (cross-disciplinary encouraged)
        Prize: HK$30,000 + fast-track to Deloitte graduate recruitment
        
        Application deadline: June 25, 2026
        Case release: July 1, 2026
        Presentation finals: August 5, 2026
        
        Apply: deloitte.com/hk/student-programs
        
        #Deloitte #CaseCompetition #Consulting #GraduateCareers
        """,
        "poster": "Deloitte Hong Kong",
        "posted_date": "2026-06-01",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_005",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/hku-entrepreneurship",
        "raw_text": """
        🌟 HKU iDendron Student Startup Competition — Applications Open!
        
        Turn your idea into a real startup with HKU's flagship entrepreneurship 
        competition. Open to all HKU students.
        
        What you get:
        - Up to HK$100,000 seed funding
        - 3-month incubation programme
        - Mentorship from industry leaders
        - Office space at Cyberport
        
        No prior business experience needed — just a great idea and determination.
        
        Application deadline: June 30, 2026
        Info session: June 10, 2026, 6pm, HKU i.lab
        
        Apply: innovation.hku.hk/idendron
        
        Open to all faculties and year groups.
        
        #HKUEntrepreneurship #Startup #iDendron #Innovation
        """,
        "poster": "HKU Technology Transfer Office",
        "posted_date": "2026-06-02",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_006",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/cfa-hk",
        "raw_text": """
        📊 CFA Institute Research Challenge 2026-27 — Team Registration Open
        
        The CFA Institute Research Challenge is the world's most prestigious 
        student investment research competition.
        
        Format: Teams of 3-5 analyse a publicly listed company and present 
        to a panel of CFA charterholders.
        
        Open to: Undergraduate and postgraduate students (all universities)
        Particularly suitable for: Finance, Economics, Accounting students
        Regional competition: November 2026
        Global finals: Early 2027
        
        Team registration deadline: August 31, 2026
        
        Register your team: cfainstitute.org/research-challenge
        
        Past HKU teams have advanced to global finals three years running.
        
        #CFAResearchChallenge #Finance #Investment #StudentCompetition
        """,
        "poster": "CFA Society Hong Kong",
        "posted_date": "2026-06-03",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_007",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/aws-educate-hk",
        "raw_text": """
        ☁️ AWS DeepRacer Student League — HKU Campus Challenge
        
        Learn machine learning by racing autonomous cars in a virtual environment!
        
        No prior ML experience required — AWS provides all training materials.
        Open to all HKU students regardless of major.
        
        Weekly time trials: Every Saturday, June-August 2026
        Campus finals: August 22, 2026
        
        Top 3 HKU finalists advance to HK Regional Championship.
        Prizes include AWS credits, merchandise, and internship referrals.
        
        Free registration: student.deepracer.com
        
        #AWSDeepRacer #MachineLearning #HKU #StudentChallenge
        """,
        "poster": "AWS Educate Hong Kong",
        "posted_date": "2026-06-04",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_008",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/pwc-hk-graduate",
        "raw_text": """
        💼 PwC Graduate Programme 2027 — Applications Now Open
        
        PwC Hong Kong is recruiting for our 2027 graduate intake across:
        - Assurance
        - Tax
        - Advisory (Deals, Consulting, Risk)
        - Technology
        
        Open to: Final year undergraduates and fresh graduates
        Application deadline: July 31, 2026
        Assessment centres: September-October 2026
        Start date: August 2027
        
        We welcome applicants from ALL degree disciplines.
        What matters: analytical thinking, communication, and drive.
        
        Apply: pwc.com/hk/careers
        
        #PwC #GraduateJobs #HongKong #BigFour #Careers
        """,
        "poster": "PwC Hong Kong",
        "posted_date": "2026-06-02",
        "scraped_at": SCRAPED_AT
    },
    {
        "id": "li_010",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/posts/hku-robotics-team",
        "raw_text": """
        🤖 HKU Robotics Team — Recruiting New Members for 2026-27!
        
        The HKU Robotics Team is looking for passionate students to join us 
        for the upcoming competition season including ABU Robocon 2027.
        
        Roles available:
        - Mechanical Engineering
        - Electronics and Control Systems
        - Software and AI
        - Team Management and Logistics
        
        Open to all years and faculties — no prior robotics experience required!
        Interest meeting: June 12, 2026, 7pm, Engineering Building Room 201
        
        Contact: robotics@hku.hk
        
        #HKURobotics #Robocon #Engineering #StudentClubrecruitment
        """,
        "poster": "HKU Robotics Team",
        "posted_date": "2026-06-03",
        "scraped_at": SCRAPED_AT
    }
]


def get_mock_linkedin_posts() -> list:
    """Returns all mock LinkedIn posts."""
    return MOCK_LINKEDIN_POSTS
