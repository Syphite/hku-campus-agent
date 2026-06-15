"""
scripts/test_connections.py
Run this first to verify all Azure services are reachable.
Usage: python3 scripts/test_connections.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

print("=" * 60)
print("HKU Campus Agent — Connection Tests")
print("=" * 60)

errors = []

# ── 1. Azure AI Search ──────────────────────────────────────────
print("\n[1/3] Testing Azure AI Search...")
try:
    from azure.search.documents.indexes import SearchIndexClient
    from azure.core.credentials import AzureKeyCredential

    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    key      = os.environ["AZURE_SEARCH_API_KEY"]

    client  = SearchIndexClient(endpoint, AzureKeyCredential(key))
    indexes = [i.name for i in client.list_indexes()]
    print(f"  OK  Connected to {endpoint}")
    print(f"  OK  Existing indexes: {indexes if indexes else '(none yet)'}")
except KeyError as e:
    msg = f"  FAIL  Missing env var: {e}"
    print(msg); errors.append(msg)
except Exception as e:
    msg = f"  FAIL  {e}"
    print(msg); errors.append(msg)

# ── 2. Azure Cosmos DB ──────────────────────────────────────────
print("\n[2/3] Testing Azure Cosmos DB...")
try:
    from azure.cosmos import CosmosClient

    endpoint = os.environ["COSMOS_ENDPOINT"]
    key      = os.environ["COSMOS_KEY"]
    database = os.environ["COSMOS_DATABASE"]

    cosmos     = CosmosClient(endpoint, key)
    db         = cosmos.get_database_client(database)
    containers = [c["id"] for c in db.list_containers()]
    print(f"  OK  Connected to {endpoint}")
    print(f"  OK  Database '{database}' containers: {containers if containers else '(none yet)'}")
except KeyError as e:
    msg = f"  FAIL  Missing env var: {e}"
    print(msg); errors.append(msg)
except Exception as e:
    msg = f"  FAIL  {e}"
    print(msg); errors.append(msg)

# ── 3. Azure OpenAI ─────────────────────────────────────────────
print("\n[3/3] Testing Azure OpenAI...")
try:
    from openai import AzureOpenAI

    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-12-01-preview"
    )
    # Minimal call to verify credentials and deployment
    resp = client.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        messages=[{"role": "user", "content": "Reply with OK"}],
        max_tokens=5
    )
    print(f"  OK  Model responded: {resp.choices[0].message.content.strip()}")
except KeyError as e:
    msg = f"  FAIL  Missing env var: {e}"
    print(msg); errors.append(msg)
except Exception as e:
    msg = f"  FAIL  {e}"
    print(msg); errors.append(msg)

# ── Summary ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
if errors:
    print(f"RESULT: {len(errors)} service(s) failed. Fix before proceeding.")
    sys.exit(1)
else:
    print("RESULT: All services connected. Ready to run scrapers.")
