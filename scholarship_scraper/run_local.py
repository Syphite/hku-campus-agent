"""
scholarship_scraper/run_local.py
Runs the full scrape + index pipeline locally against real Azure AI Search.
Usage: python3 run_local.py [--limit N]
"""

import sys, os, json, logging
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

logging.basicConfig(level=logging.INFO)

from parser  import scrape_all
from indexer import ensure_index_exists, upsert_scholarships, get_existing_ids, touch_index_metadata

limit = None
if "--limit" in sys.argv:
    idx = sys.argv.index("--limit")
    limit = int(sys.argv[idx + 1])

print(f"Running scholarship scraper (limit={limit or 'all'})...")
ensure_index_exists()
existing = get_existing_ids()
print(f"Existing docs in index: {len(existing)}")

scholarships = scrape_all(limit=limit, delay=0.8)
print(f"Scraped: {len(scholarships)}")

new_ids = {s["id"] for s in scholarships} - existing
print(f"New since last run: {len(new_ids)}")

succeeded, failed = upsert_scholarships(scholarships)
if scholarships:
    touch_index_metadata("scholarship_scrape_local")
print(f"\nDone. Indexed: {succeeded} succeeded, {failed} failed")

if scholarships:
    print("\nSample (first 2):")
    for s in scholarships[:2]:
        print(json.dumps({k: s[k] for k in ["id","name","faculty","level","gpa_requirement","deadline_raw","merit_based","financial_need"]}, indent=2))
