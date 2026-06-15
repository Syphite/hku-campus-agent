"""
scholarship_scraper/validate_test.py
Local test — scrapes 30 scholarships, validates fields, prints report.
No Azure needed. Run: python3 validate_test.py
"""

import sys, time, re
sys.path.insert(0, '.')
from parser import parse_scholarship_page, CURRENT_YEAR
from dataclasses import asdict

SAMPLE_IDS = [
    396, 841, 663, 662, 803, 839, 414, 413, 840, 472,
    766, 62,  144, 117, 486, 273, 786, 838, 837, 836,
    781, 782, 716, 731, 487, 583, 541, 835, 48,  473
]

def validate(s: dict) -> list:
    w = []
    dl  = s.get("deadline_raw", "")
    con = s.get("deadline_confidence", "none")
    ym  = re.search(r"\d{4}", dl)
    if ym and int(ym.group()) < CURRENT_YEAR - 1:
        w.append(f"Deadline '{dl}' looks like a past date — verify")
    if con == "none":
        w.append("No deadline extracted (likely unpublished — not a bug)")
    if con == "low":
        w.append(f"Deadline '{dl}' low confidence — verify manually")
    gpa = s.get("gpa_requirement")
    if gpa and (gpa > 4.3 or gpa < 2.0):
        w.append(f"GPA {gpa} out of range")
    if not any([s.get("financial_need"), s.get("merit_based"), s.get("is_entrance"), s.get("is_enrichment")]):
        w.append("No scholarship type detected")
    return w

print(f"Scraping {len(SAMPLE_IDS)} scholarships...\n")
scholarships = []
for i, ss_id in enumerate(SAMPLE_IDS):
    try:
        print(f"  [{i+1:02}/{len(SAMPLE_IDS)}] ss_id={ss_id}", end=" ", flush=True)
        s = parse_scholarship_page(ss_id)
        scholarships.append(asdict(s))
        print(f"OK — {s.name[:50]}")
        time.sleep(0.8)
    except Exception as e:
        print(f"FAILED: {e}")

clean, flagged = [], []
for s in scholarships:
    w = validate(s)
    (flagged if w else clean).append({"s": s, "w": w})

print(f"\n{'='*70}")
print(f"RESULTS: {len(clean)} clean  |  {len(flagged)} flagged  |  {len(SAMPLE_IDS)-len(scholarships)} failed to fetch")
print(f"{'='*70}")

if flagged:
    print("\nFLAGGED:")
    for f in flagged:
        s = f["s"]
        print(f"\n  {s['name'][:60]}")
        print(f"    faculty={s['faculty']}  level={s['level']}  year={s['year_of_study']}")
        print(f"    nationality={s['nationality']}  gpa={s['gpa_requirement']}")
        print(f"    merit={s['merit_based']}  need={s['financial_need']}  entrance={s['is_entrance']}  enrichment={s['is_enrichment']}")
        print(f"    deadline='{s['deadline_raw']}' (conf={s['deadline_confidence']})")
        print(f"    amount='{s['amount']} {s['currency']}'")
        for warning in f["w"]:
            print(f"    *** {warning}")

print(f"\nCLEAN:")
for f in clean:
    s = f["s"]
    print(f"  OK  {s['name'][:50]:<50} | {s['deadline_raw'][:25]:<25} | gpa={s['gpa_requirement']}")
