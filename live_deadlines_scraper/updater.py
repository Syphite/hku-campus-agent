"""
live_deadlines_scraper/updater.py
Updates Azure AI Search index with live deadline data.
"""

import os, logging
from datetime import datetime, timezone, date
from typing import Optional
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import QueryType

logger     = logging.getLogger(__name__)
ENDPOINT   = os.environ["AZURE_SEARCH_ENDPOINT"]
API_KEY    = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME = os.environ.get("SCHOLARSHIP_INDEX_NAME", "scholarships")


def get_client() -> SearchClient:
    return SearchClient(ENDPOINT, INDEX_NAME, AzureKeyCredential(API_KEY))


def deadlines_differ(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b or a == b: return False
    if len(a) == 10 and len(b) == 10:
        try:
            return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) > 2
        except ValueError: pass
    return a.strip().lower() != b.strip().lower()


def check_discrepancy(existing: dict, live: dict) -> Optional[dict]:
    flags = []
    if deadlines_differ(existing.get("deadline_iso"), live.get("deadline_iso")):
        flags.append({"field": "deadline",
            "message": f"Deadline changed from '{existing.get('deadline_raw')}' to '{live.get('deadline_raw')}'. Please verify."})
    if existing.get("is_rolling") != live.get("is_rolling"):
        flags.append({"field": "is_rolling", "message": "Scholarship basis changed (rolling vs fixed). Please verify."})
    if live.get("form_url") and live.get("form_url") != existing.get("form_url"):
        flags.append({"field": "form_url", "message": f"Application form URL changed. New form: {live.get('form_url')}"})
    if not flags: return None
    return {"scholarship_name": live["name"], "ss_id": live.get("ss_id"),
            "flags": flags, "detected_at": datetime.now(timezone.utc).isoformat()}


def get_by_ss_id(client, ss_id: int) -> Optional[dict]:
    try:
        results = list(client.search(search_text="*", filter=f"ss_id eq {ss_id}",
            select=["id","name","deadline_raw","deadline_iso","is_open","is_rolling","form_url","application_method"], top=1))
        return results[0] if results else None
    except Exception as e:
        logger.error(f"Error fetching ss_id={ss_id}: {e}"); return None


def get_by_name(client, name: str) -> Optional[dict]:
    try:
        results = list(client.search(search_text=name, query_type=QueryType.SIMPLE,
            select=["id","name","deadline_raw","deadline_iso","is_open","is_rolling","form_url"], top=1))
        if results:
            wa = set(results[0].get("name","").lower().split())
            wb = set(name.lower().split())
            if len(wa & wb) / max(len(wa | wb), 1) >= 0.6:
                return results[0]
    except Exception as e:
        logger.error(f"Error by name '{name}': {e}")
    return None


def mark_closed(client, ids: set) -> int:
    closed = 0
    for doc_id in ids:
        try:
            client.merge_or_upload_documents([{"id": doc_id, "is_open": False,
                "deadline_raw": "Application window closed", "deadline_confidence": "high"}])
            closed += 1
        except Exception as e:
            logger.error(f"Error closing {doc_id}: {e}")
    return closed


def update_index_with_live_deadlines(live_results: dict) -> dict:
    client       = get_client()
    updated      = created = 0
    discrepancies = []
    live_ids     = set()

    for live in live_results["hku_open"]:
        ss_id = live.get("ss_id")
        if not ss_id: continue
        existing = get_by_ss_id(client, ss_id)
        if existing:
            live_ids.add(existing["id"])
            d = check_discrepancy(existing, live)
            if d: discrepancies.append(d)
            client.merge_or_upload_documents([{"id": existing["id"], "is_open": True,
                "deadline_raw": live["deadline_raw"], "deadline_iso": live.get("deadline_iso"),
                "deadline_confidence": "high", "is_rolling": live["is_rolling"],
                "application_method": live["application_method"], "form_url": live.get("form_url"),
                "live_page_updated": live_results["page_last_updated"]}])
            updated += 1
        else:
            doc_id = f"ss_{ss_id}"
            client.merge_or_upload_documents([{"id": doc_id, "ss_id": ss_id, "name": live["name"],
                "source": "live_only", "is_open": True, "deadline_raw": live["deadline_raw"],
                "deadline_iso": live.get("deadline_iso"), "deadline_confidence": "high",
                "is_rolling": live["is_rolling"], "application_url": live["application_url"],
                "application_method": live["application_method"], "form_url": live.get("form_url"),
                "level": [live["level"]], "eligibility_raw": "Visit the scholarship page for full details.",
                "live_page_updated": live_results["page_last_updated"], "scraped_at": live["scraped_at"]}])
            live_ids.add(doc_id); created += 1

    for live in live_results["external_open"]:
        existing = get_by_name(client, live["name"])
        if existing:
            live_ids.add(existing["id"])
            d = check_discrepancy(existing, live)
            if d: discrepancies.append(d)
            client.merge_or_upload_documents([{"id": existing["id"], "is_open": True,
                "deadline_raw": live["deadline_raw"], "deadline_iso": live.get("deadline_iso"),
                "deadline_confidence": "high", "is_rolling": live["is_rolling"],
                "application_url": live["application_url"], "application_method": live["application_method"],
                "live_page_updated": live_results["page_last_updated"]}])
            updated += 1
        else:
            ext_id = f"ext_{abs(hash(live['name'])) % 100000}"
            client.merge_or_upload_documents([{"id": ext_id, "name": live["name"],
                "source": "external_live", "is_open": True, "deadline_raw": live["deadline_raw"],
                "deadline_iso": live.get("deadline_iso"), "deadline_confidence": "high",
                "is_rolling": live["is_rolling"], "application_url": live["application_url"],
                "application_method": live["application_method"], "level": [live["level"]],
                "eligibility_raw": "External scholarship. Visit the scholarship website for full eligibility details.",
                "live_page_updated": live_results["page_last_updated"], "scraped_at": live["scraped_at"]}])
            live_ids.add(ext_id); created += 1

    try:
        prev = {r["id"] for r in client.search(search_text="*", filter="is_open eq true", select=["id"], top=1000)}
        closed = mark_closed(client, prev - live_ids)
    except Exception as e:
        logger.error(f"Error marking closed: {e}"); closed = 0

    summary = {"updated": updated, "created": created, "closed": closed,
               "discrepancies": discrepancies, "run_at": datetime.now(timezone.utc).isoformat()}
    logger.info(f"Live update: {updated} updated, {created} created, {closed} closed, {len(discrepancies)} discrepancies")
    return summary
