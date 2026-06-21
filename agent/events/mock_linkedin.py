"""
agent/events/mock_linkedin.py
Mock LinkedIn post data simulating what a real LinkedIn scraper would return.
20 varied posts with metadata for keyword pre-filtering.
"""

from datetime import datetime, timezone

SCRAPED_AT = datetime.now(timezone.utc).isoformat()


def _post(
    post_id: str,
    poster: str,
    url_slug: str,
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
        "source": "linkedin",
        "source_url": f"https://www.linkedin.com/posts/{url_slug}",
        "raw_text": raw_text.strip(),
        "poster": poster,
        "posted_date": posted_date,
        "scraped_at": SCRAPED_AT,
        "keywords": keywords,
        "faculty_tags": faculty_tags or ["all"],
        "year_tags": year_tags or ["all"],
        "is_noise": is_noise,
    }


MOCK_LINKEDIN_POSTS = [
    _post(
        "li_001", "Microsoft Hong Kong", "microsoft-hk",
        """
        Microsoft Imagine Cup 2026 — Hong Kong Regional is NOW OPEN!
        Build AI solutions with Azure. Open to all HK university students, teams of 1-4.
        Grand prize: HK$50,000 + trip to global finals. Deadline: July 15, 2026.
        Skills: AI/ML, cloud computing, app development.
        #ImagineCup #Microsoft #StudentInnovation #HongKong
        """,
        "2026-06-01",
        keywords=["ai", "machine learning", "hackathon", "cloud computing", "innovation"],
        faculty_tags=["engineering", "science", "all"],
    ),
    _post(
        "li_002", "Google Developer Student Clubs HK", "google-hk",
        """
        Google Solution Challenge 2026 — build for UN SDGs using Google technologies.
        University students worldwide, teams of 1-4. Deadline: July 31, 2026.
        Great for CS, Engineering, and Data Science students.
        #GoogleSolutionChallenge #SDGs #StudentDeveloper
        """,
        "2026-06-02",
        keywords=["software engineering", "data", "sustainability", "hackathon"],
        faculty_tags=["engineering", "science", "all"],
    ),
    _post(
        "li_003", "HKUST FinTech Institute", "hkust-fintech",
        """
        HKUST FinTech Competition 2026 — Blockchain, AI in finance, RegTech, Insurtech.
        Undergraduate and postgraduate students from all HK universities. Teams 2-5.
        Prizes totalling HK$200,000. Submission deadline: July 20, 2026.
        #FinTech #Blockchain #Finance #StudentCompetition
        """,
        "2026-06-03",
        keywords=["fintech", "blockchain", "finance", "consulting", "competition"],
        faculty_tags=["business", "economics", "engineering"],
    ),
    _post(
        "li_004", "Deloitte Hong Kong", "deloitte-hk",
        """
        Deloitte Technology Fast 500 Student Case Competition 2026.
        Final year undergraduate and postgraduate students. Digital transformation focus.
        Prize: HK$30,000 + fast-track to Deloitte graduate recruitment.
        Application deadline: June 25, 2026.
        #Deloitte #CaseCompetition #Consulting #GraduateCareers
        """,
        "2026-06-01",
        keywords=["consulting", "case competition", "business", "strategy", "career"],
        faculty_tags=["business", "economics", "all"],
        year_tags=["4", "master", "all"],
    ),
    _post(
        "li_005", "HKU Technology Transfer Office", "hku-entrepreneurship",
        """
        HKU iDendron Student Startup Competition — up to HK$100,000 seed funding,
        3-month incubation, mentorship at Cyberport. All HKU students, all faculties.
        Application deadline: June 30, 2026. Info session June 10 at HKU i.lab.
        #HKUEntrepreneurship #Startup #Innovation
        """,
        "2026-06-02",
        keywords=["entrepreneurship", "startup", "innovation", "business"],
        faculty_tags=["all"],
    ),
    _post(
        "li_006", "CFA Society Hong Kong", "cfa-hk",
        """
        CFA Institute Research Challenge 2026-27 — teams analyse a listed company.
        Open to undergraduate and postgraduate students. Finance, Economics, Accounting ideal.
        Team registration deadline: August 31, 2026.
        #CFAResearchChallenge #Finance #Investment
        """,
        "2026-06-03",
        keywords=["finance", "investment", "economics", "accounting", "research"],
        faculty_tags=["business", "economics"],
    ),
    _post(
        "li_007", "AWS Educate Hong Kong", "aws-educate-hk",
        """
        AWS DeepRacer Student League — HKU Campus Challenge.
        Learn machine learning by racing autonomous cars. No prior ML required.
        Weekly trials June-August 2026. Campus finals August 22.
        #AWSDeepRacer #MachineLearning #HKU
        """,
        "2026-06-04",
        keywords=["machine learning", "ai", "robotics", "cloud computing", "workshop"],
        faculty_tags=["engineering", "science", "all"],
    ),
    _post(
        "li_008", "PwC Hong Kong", "pwc-hk-graduate",
        """
        PwC Graduate Programme 2027 — Assurance, Tax, Advisory, Technology tracks.
        Final year undergraduates and fresh graduates. Application deadline: July 31, 2026.
        All degree disciplines welcome.
        #PwC #GraduateJobs #BigFour #Careers
        """,
        "2026-06-02",
        keywords=["graduate jobs", "career", "accounting", "consulting", "recruitment"],
        faculty_tags=["business", "all"],
        year_tags=["4", "master", "all"],
    ),
    _post(
        "li_009", "HKU Law Society", "hku-law-moot",
        """
        Philip C. Jessup International Law Moot Court Competition 2026 — HKU team trials.
        Open to LLB and JD students with strong legal research and advocacy skills.
        Internal selection deadline: June 18, 2026. Oral rounds in August.
        #LawMoot #LegalResearch #HKU #Jessup
        """,
        "2026-06-05",
        keywords=["law", "legal research", "moot court", "debate"],
        faculty_tags=["law"],
        year_tags=["2", "3", "4", "all"],
    ),
    _post(
        "li_010", "HKU Robotics Team", "hku-robotics-team",
        """
        HKU Robotics Team recruiting for ABU Robocon 2027 season.
        Roles: mechanical, electronics, software/AI, logistics. All years welcome.
        Interest meeting: June 12, 2026, Engineering Building Room 201.
        #HKURobotics #Robocon #Engineering
        """,
        "2026-06-03",
        keywords=["robotics", "engineering", "mechanical", "software engineering", "competition"],
        faculty_tags=["engineering"],
    ),
    _post(
        "li_011", "HKU Faculty of Medicine", "hku-med-research",
        """
        Summer Clinical Research Attachment Programme 2026 — Faculty of Medicine.
        Undergraduate medical and biomedical students. 8-week lab placements in Queen Mary Hospital.
        Application deadline: June 22, 2026. Stipend provided.
        #MedicalResearch #HKUMed #ClinicalAttachment
        """,
        "2026-06-04",
        keywords=["medicine", "clinical", "research", "health", "biomedical"],
        faculty_tags=["medicine"],
        year_tags=["2", "3", "4"],
    ),
    _post(
        "li_012", "Hong Kong Arts Development Council", "hkadc-young-artists",
        """
        Young Artist Scheme 2026 — grants for emerging performers and visual artists.
        Open to HK tertiary students in music, theatre, dance, and fine arts.
        Grant up to HK$40,000. Application deadline: July 10, 2026.
        #Arts #PerformingArts #VisualArts #HKADC
        """,
        "2026-06-06",
        keywords=["arts", "music", "theatre", "creative", "performance"],
        faculty_tags=["arts"],
    ),
    _post(
        "li_013", "UNESCO Hong Kong Association", "unesco-hk-youth",
        """
        UNESCO HK Youth Peace Ambassador Programme 2026.
        Workshops on SDGs, cultural diplomacy, and community projects.
        Open to all HK tertiary students. Programme runs July-August 2026.
        Apply by June 28, 2026.
        #UNESCO #Peace #SDGs #Volunteering
        """,
        "2026-06-05",
        keywords=["volunteering", "sustainability", "cultural exchange", "community"],
        faculty_tags=["all"],
    ),
    _post(
        "li_014", "Hong Kong Institute of Architects", "hkia-student",
        """
        HKIA Student Design Charrette 2026 — 48-hour urban design challenge.
        Architecture and landscape students from HK universities. Teams of 3-4.
        Registration deadline: June 20, 2026. Charrette: July 5-7.
        #Architecture #UrbanDesign #HKIA
        """,
        "2026-06-07",
        keywords=["architecture", "design", "urban planning", "competition"],
        faculty_tags=["architecture", "engineering"],
    ),
    _post(
        "li_015", "Hong Kong Red Cross", "hkrc-youth-volunteer",
        """
        Red Cross Youth Humanitarian Leadership Camp 2026.
        First aid, disaster response training, and community service projects.
        Open to all HK tertiary students. Camp dates: July 8-12. Apply by June 15.
        #Volunteering #FirstAid #Leadership #RedCross
        """,
        "2026-06-04",
        keywords=["volunteering", "leadership", "community", "humanitarian"],
        faculty_tags=["all"],
    ),
    _post(
        "li_016", "Bloomberg LP Hong Kong", "bloomberg-datathon",
        """
        Bloomberg Data Datathon 2026 — analyse ESG and market datasets.
        Finance, economics, and data science students preferred. Teams of 2-4.
        Finals at Bloomberg HK office. Register by July 5, 2026.
        #Bloomberg #DataScience #Finance #ESG
        """,
        "2026-06-08",
        keywords=["data analysis", "finance", "economics", "statistics", "competition"],
        faculty_tags=["business", "economics", "science", "engineering"],
    ),
    _post(
        "li_017", "HKU Faculty of Education", "hku-education-outreach",
        """
        Teaching Outreach Fellowship 2026 — support local secondary school STEM workshops.
        Education and science students welcome. 6-week part-time placement in July-August.
        Apply by June 19, 2026. Certificate and stipend provided.
        #Education #STEM #Teaching #Outreach
        """,
        "2026-06-06",
        keywords=["education", "teaching", "stem", "outreach", "workshop"],
        faculty_tags=["education", "science", "engineering"],
    ),
    _post(
        "li_018", "Hong Kong Journalists Association", "hkjaa-student",
        """
        Student Newsroom Fellowship 2026 — reporting workshops and newsroom shadowing.
        Journalism, media, and communications students. Bilingual English/Cantonese preferred.
        Application deadline: June 24, 2026.
        #Journalism #Media #Communications #Reporting
        """,
        "2026-06-07",
        keywords=["journalism", "media", "communications", "writing", "reporting"],
        faculty_tags=["arts", "social sciences"],
    ),
    _post(
        "li_019", "Hong Kong Sports Institute", "hksi-student-athlete",
        """
        University Athlete Development Programme 2026 — training camps and sports science seminars.
        Varsity team members and high-performance student athletes from HK universities.
        Registration by June 17, 2026. Programme July 1-15.
        #Sports #Athletics #StudentAthlete #Training
        """,
        "2026-06-05",
        keywords=["sports", "athletics", "fitness", "training"],
        faculty_tags=["all"],
    ),
    _post(
        "li_020", "LinkedIn Premium Promo", "linkedin-premo-sale",
        """
        LinkedIn Premium Career — 50% off for 3 months! Upgrade your profile visibility,
        see who viewed your profile, and access LinkedIn Learning courses.
        Limited time offer. Terms apply. Not a student event.
        #LinkedInPremium #Sale #Career
        """,
        "2026-06-09",
        keywords=["promo", "sale", "discount", "advertisement"],
        faculty_tags=["all"],
        is_noise=True,
    ),
]


def get_mock_linkedin_posts() -> list:
    return MOCK_LINKEDIN_POSTS
