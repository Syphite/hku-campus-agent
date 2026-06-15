"""
scholarship_scraper/indexer.py
Creates the Azure AI Search index and upserts scholarship documents.
"""

import os
import logging
from datetime import datetime, timezone
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex, SearchField, SearchFieldDataType,
    SimpleField, SearchableField,
)
from azure.search.documents.models import IndexingResult

logger     = logging.getLogger(__name__)
ENDPOINT   = os.environ["AZURE_SEARCH_ENDPOINT"]
API_KEY    = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME = os.environ.get("SCHOLARSHIP_INDEX_NAME", "scholarships")


def get_index_schema() -> SearchIndex:
    fields = [
        SimpleField(name="id",                   type=SearchFieldDataType.String,  key=True),
        SimpleField(name="ss_id",                type=SearchFieldDataType.Int32,   filterable=True),
        SearchableField(name="name",             type=SearchFieldDataType.String,  sortable=True),
        SimpleField(name="source_url",           type=SearchFieldDataType.String),
        SimpleField(name="provider",             type=SearchFieldDataType.String,  filterable=True),
        SearchableField(name="value_raw",        type=SearchFieldDataType.String),
        SearchableField(name="amount",           type=SearchFieldDataType.String),
        SimpleField(name="currency",             type=SearchFieldDataType.String,  filterable=True),

        # Eligibility filters
        SimpleField(name="is_entrance",          type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="is_enrichment",        type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="financial_need",       type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="merit_based",          type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="renewable",            type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="place_of_origin",      type=SearchFieldDataType.String,  filterable=True),
        SimpleField(name="gpa_requirement",      type=SearchFieldDataType.Double,  filterable=True, sortable=True),

        # Live deadline fields (updated daily)
        SimpleField(name="is_open",              type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="is_rolling",           type=SearchFieldDataType.Boolean, filterable=True),
        SimpleField(name="deadline_iso",         type=SearchFieldDataType.String,  filterable=True, sortable=True),
        SearchableField(name="deadline_raw",     type=SearchFieldDataType.String),
        SimpleField(name="deadline_confidence",  type=SearchFieldDataType.String,  filterable=True),
        SimpleField(name="form_url",             type=SearchFieldDataType.String),
        SearchableField(name="application_method", type=SearchFieldDataType.String),
        SimpleField(name="application_url",      type=SearchFieldDataType.String),
        SimpleField(name="live_page_updated",    type=SearchFieldDataType.String),
        SimpleField(name="source",               type=SearchFieldDataType.String,  filterable=True),
        SimpleField(name="duration",             type=SearchFieldDataType.String, filterable=True),

        # Collection fields
        SearchField(name="faculty",              type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True, searchable=True),
        SearchField(name="level",                type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        SearchField(name="year_of_study",        type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        SearchField(name="nationality",          type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
        SearchField(name="submission_materials", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),

        # Prose for LLM reasoning
        SearchableField(name="eligibility_raw",  type=SearchFieldDataType.String),
        SearchableField(name="renewal_conditions", type=SearchFieldDataType.String),

        # Metadata
        SimpleField(name="last_updated",         type=SearchFieldDataType.String),
        SimpleField(name="scraped_at",           type=SearchFieldDataType.String),
    ]
    return SearchIndex(name=INDEX_NAME, fields=fields)


def ensure_index_exists():
    client   = SearchIndexClient(ENDPOINT, AzureKeyCredential(API_KEY))
    existing = [i.name for i in client.list_indexes()]
    if INDEX_NAME not in existing:
        logger.info(f"Creating index '{INDEX_NAME}'...")
        client.create_index(get_index_schema())
        logger.info("Index created.")
    else:
        logger.info(f"Index '{INDEX_NAME}' already exists.")


def upsert_scholarships(scholarships: list) -> tuple:
    client    = SearchClient(ENDPOINT, INDEX_NAME, AzureKeyCredential(API_KEY))
    succeeded = failed = 0
    for i in range(0, len(scholarships), 100):
        batch = scholarships[i:i+100]
        try:
            results = client.merge_or_upload_documents(batch)
            for r in results:
                if r.succeeded: succeeded += 1
                else:
                    failed += 1
                    logger.error(f"Failed {r.key}: {r.error_message}")
        except Exception as e:
            logger.error(f"Batch error: {e}")
            failed += len(batch)
    logger.info(f"Indexed: {succeeded} succeeded, {failed} failed")
    return succeeded, failed


def get_existing_ids() -> set:
    client  = SearchClient(ENDPOINT, INDEX_NAME, AzureKeyCredential(API_KEY))
    results = client.search(search_text="*", select=["id"], top=1000)
    return {doc["id"] for doc in results}
