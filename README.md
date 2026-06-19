# HKU Campus Intelligence Agent — Scraper Pipeline

## Project Structure
```
hku_agent/
├── .env.example                    ← copy to .env and fill in your keys
├── .gitignore
├── requirements.txt
├── README.md
├── scripts/
│   ├── test_connections.py         ← run this first
│   └── seed_profiles.py            ← retired; profiles come from onboarding
├── scholarship_scraper/
│   ├── parser.py                   ← scrapes scholar.aas.hku.hk (665 scholarships)
│   ├── indexer.py                  ← creates/updates Azure AI Search index
│   ├── validate_test.py            ← local test, no Azure needed
│   └── run_local.py                ← full scrape + index, needs Azure
├── live_deadlines_scraper/
│   ├── parser.py                   ← scrapes aas.hku.hk/apply-scholarships/
│   └── updater.py                  ← updates index with live deadlines
├── external_schemes_scraper/
│   └── parser.py                   ← scrapes aas.hku.hk/external-schemes/
└── tests/
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in .env
cp .env.example .env
# Edit .env with your real Azure keys

# 3. Test all connections
python3 scripts/test_connections.py
```

## Testing (no Azure needed)

```bash
# Test scholarship scraper against live HKU site
cd scholarship_scraper
python3 validate_test.py

# Test live deadlines parser
cd live_deadlines_scraper
python3 parser.py

# Test external schemes parser
cd external_schemes_scraper
python3 parser.py
```

## Running with Azure

```bash
# Scrape 5 scholarships and index them (quick test)
cd scholarship_scraper
python3 run_local.py --limit 5

# Scrape all 665 and index (takes ~15 minutes)
python3 run_local.py

# Create user profiles through the Copilot onboarding card
```
