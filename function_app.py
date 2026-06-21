"""
function_app.py
Single Azure Function App hosting all scraper timer triggers.
"""
import logging
import azure.functions as func

# Import your scraper logic from the subfolders
from scholarship_scraper.parser import scrape_all
from scholarship_scraper.indexer import ensure_index_exists, upsert_scholarships, touch_index_metadata
from live_deadlines_scraper.parser import scrape_live_deadlines
from live_deadlines_scraper.updater import update_index_with_live_deadlines
from external_schemes_scraper.parser import parse_external_schemes

app = func.FunctionApp()
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# 1. Weekly HKU Scholarship Scraper (Mondays at 00:00 UTC / 08:00 HKT)
# -----------------------------------------------------------------------
@app.timer_trigger(schedule="0 0 0 * * 1", arg_name="timer", run_on_startup=False, use_monitor=True)
def scrape_scholarships(timer: func.TimerRequest) -> None:
    logger.info("Starting weekly scholarship scrape...")
    ensure_index_exists()
    scholarships = scrape_all(limit=None, delay=1.0)
    if scholarships:
        succeeded, failed = upsert_scholarships(scholarships)
        touch_index_metadata("scholarship_scrape")
        logger.info(f"Scholarships: Indexed {succeeded} succeeded, {failed} failed")

# -----------------------------------------------------------------------
# 2. Daily Live Deadlines Scraper (Every day at 01:00 UTC / 09:00 HKT)
# -----------------------------------------------------------------------
@app.timer_trigger(schedule="0 0 1 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def update_live_deadlines(timer: func.TimerRequest) -> None:
    logger.info("Starting daily live deadline update...")
    live_results = scrape_live_deadlines()
    if live_results and (live_results.get("hku_open") or live_results.get("external_open")):
        summary = update_index_with_live_deadlines(live_results)
        touch_index_metadata("live_deadlines")
        logger.info(f"Live deadlines updated: {summary}")

# -----------------------------------------------------------------------
# 3. Weekly External Schemes Scraper (Mondays at 02:00 UTC / 10:00 HKT)
# -----------------------------------------------------------------------
@app.timer_trigger(schedule="0 0 2 * * 1", arg_name="timer", run_on_startup=False, use_monitor=True)
def scrape_external_schemes(timer: func.TimerRequest) -> None:
    logger.info("Starting weekly external schemes scrape...")
    ensure_index_exists()
    schemes = parse_external_schemes()
    if schemes:
        succeeded, failed = upsert_scholarships(schemes)
        touch_index_metadata("external_schemes")
        logger.info(f"External schemes: Indexed {succeeded} succeeded, {failed} failed")
