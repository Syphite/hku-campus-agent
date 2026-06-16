"""
scripts/seed_profiles.py
Seeds the three demo personas into Cosmos DB.
Usage: python3 scripts/seed_profiles.py
"""

import os, json, glob
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

import ssl, certifi
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from azure.cosmos import CosmosClient

cosmos     = CosmosClient(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"])
db         = cosmos.get_database_client(os.environ["COSMOS_DATABASE"])
container  = db.get_container_client("profiles")

persona_dir = os.path.join(os.path.dirname(__file__), '..', 'tests', 'personas')
files       = glob.glob(os.path.join(persona_dir, 'persona_*.json'))  # only persona files

print(f"Seeding {len(files)} personas into Cosmos DB...")
for f in files:
    with open(f) as fh:
        persona = json.load(fh)
    print(f"  Seeding: {persona.get('id', 'MISSING ID')} from {os.path.basename(f)}")
    container.upsert_item(body=persona)
    print(f"  Done: {persona['id']}")

print("All profiles seeded.")
