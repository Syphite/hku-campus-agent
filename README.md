# HKU Campus Agent

A Microsoft Teams / Copilot bot that acts as a proactive co-pilot for HKU students. It triages inbox email, surfaces personalised events and scholarships, checks calendar conflicts, and helps fill scholarship application forms.

Students interact via chat (`digest`, `inbox`, `events`, `scholarships`) and Adaptive Cards. The bot orchestrates Microsoft Graph, Azure OpenAI, Azure AI Search, and Cosmos DB behind the scenes.

---

## System topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Microsoft Teams / Copilot                           │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ Bot Framework Activities
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  bot_adapter.py          POST /api/messages, GET /download/{token}      │
│  • OAuth sign-in (GraphOAuth)                                           │
│  • Attachment routing (CV / application forms)                          │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  agent/handler.py              Main orchestrator & intent routing       │
│  agent/intent_router.py        Natural-language command dispatch        │
│  agent/digest.py               Assembles unified digest output          │
│  agent/proactive.py            “Since last visit” snapshot              │
└───────┬─────────────┬─────────────┬─────────────┬─────────────┬─────────┘
        │             │             │             │             │
        ▼             ▼             ▼             ▼             ▼
┌──────────────┐ ┌──────────┐ ┌────────────┐ ┌───────────┐ ┌─────────────┐
│ agent/       │ │ agent/   │ │ agent/     │ │ agent/    │ │ agent/      │
│ profile.py   │ │ graph.py │ │ matching.py│ │ email_    │ │ application/│
│              │ │          │ │            │ │ pipeline  │ │             │
│ Cosmos DB    │ │ Graph    │ │ Azure AI   │ │ +         │ │ Form parse, │
│ profiles     │ │ /me mail │ │ Search +   │ │ classifier│ │ fill, draft │
│              │ │ calendar │ │ GPT stage2 │ │           │ │             │
└──────────────┘ └──────────┘ └────────────┘ └───────────┘ └─────────────┘
        │             │             │             │             │
        │             │             ▼             │             ▼
        │             │      ┌────────────┐       │      ┌─────────────┐
        │             │      │ function_  │       │      │ Azure Blob  │
        │             │      │ app.py     │       │      │ (filled     │
        │             │      │ timer      │       │      │ applications)│
        │             │      │ scrapers   │       │      └─────────────┘
        │             │      └────────────┘       │
        │             │             │             │
        └─────────────┴─────────────┴─────────────┘
                                  │
                                  ▼
                    Azure OpenAI (GPT-4o) — classification, routing,
                    eligibility reasoning, form schema, drafts, collection
```

**Offline / batch path (not in the chat loop):** timer Azure Functions in `function_app.py` scrape HKU scholarship pages and upsert documents into Azure AI Search. Matching reads that index at digest time.

---

## Component roles

| Component | Location | Role |
|-----------|----------|------|
| **Bot adapter** | `bot_adapter.py` | HTTP entry point for Bot Framework; OAuth; sends text/cards/files back to Teams |
| **Handler** | `agent/handler.py` | Routes messages, card actions, and attachments; runs digest, inbox, events, application flows |
| **Intent router** | `agent/intent_router.py` | LLM-based parsing of free-text commands (`digest`, profile updates, etc.) |
| **Profile store** | `agent/profile.py` | Cosmos DB read from `profiles` container; stores profile, `graph_token`, caches, `application_state` |
| **Graph client** | `agent/graph.py` | Delegated Microsoft Graph: unread mail, folder moves, calendar read/write |
| **Email pipeline** | `agent/email_pipeline.py` | Fetch → dedupe → classify → archive/keep; returns inbox summary for digest |
| **Classifier** | `agent/classifier.py` | Labels each email `noise` / `urgent` / `relevant` / `ambiguous` using profile context |
| **Email → events** | `agent/email_events.py` | Converts classified inbox items into event-shaped records (digest only) |
| **Event matching** | `agent/events/event_matching.py` | Two-stage personalisation over mock social posts + extracted structure |
| **Event extractor** | `agent/events/event_extractor.py` | GPT extraction of structured fields from raw post/email text |
| **Conflict checker** | `agent/event_registration.py`, `agent/graph.py` | Calendar conflict and registration-impact warnings |
| **Scholarship matching** | `agent/matching.py` | Azure AI Search stage 1 + GPT eligibility stage 2; caches per student |
| **Scrapers** | `scholarship_scraper/`, `live_deadlines_scraper/`, `external_schemes_scraper/` | Populate/update the scholarship search index |
| **Function app** | `function_app.py` | Weekly/daily timer triggers for scrapers |
| **Application pipeline** | `agent/application/` | DOCX/PDF parse → schema analysis → conversational gap-fill → filled form output |
| **Legacy drafter** | `agent/drafter.py`, `agent/form_filler.py` | Question extraction + essay draft flow for older “Start Draft” cards |
| **Blob storage** | `agent/storage/blob_storage.py` | Upload filled application forms; return time-limited SAS download URL |
| **Digest assembler** | `agent/digest.py` | Merges scholarships, events, and inbox into one structured digest |
| **Flask demo** | `app.py` | Standalone email-classifier web UI (dev/demo; not the Teams bot) |

---

## How the features work

### 1. Inbox

Triggered by **`digest`** (stage 1) or **`inbox`**.

1. **Gate:** User must complete onboarding and sign in with Microsoft so the bot holds a delegated **`graph_token`** in Cosmos.
2. **Fetch:** `graph.py` paginates unread messages from `/me/mailFolders/inbox/messages`.
3. **Folders:** Agent creates `Agent Archived` and `Agent Ambiguous` mail folders if missing.
4. **Dedup:** Content fingerprint (sender + subject) skips emails already archived in a prior run.
5. **Classify:** Each message is labelled by `classifier.py`:
   - **noise** → moved to Agent Archived (never deleted)
   - **urgent** / **relevant** → kept in inbox for the student
   - **ambiguous** → moved to Agent Ambiguous for manual review
   - Protected senders (e.g. CEDARS) are always kept
6. **Enrich:** Kept items may get calendar timing hints via `email_calendar.py`.
7. **Persist:** Archive fingerprints are saved on the profile so repeat digests do not re-process the same mail.
8. **Render:** Handler sends summary counts, kept/urgent items, undo-archive cards, and optional “add to calendar” actions.

Requires Graph scopes **Mail.Read** (and folder move permissions via the OAuth connection).

### 2. Events — mock social media + email events

Events come from **two sources**, merged during **`digest`** (not the standalone `events` command, which uses feeds only).

#### A. Mock social feeds

`run_event_matching()` loads static post corpora:

- `agent/events/mock_linkedin.py`
- `agent/events/mock_xiaohongshu.py`

Pipeline:

1. **Stage 1 — keyword filter:** Faculty, year, and interest keywords narrow posts (mirrors scholarship matching).
2. **Extract:** `event_extractor.py` runs GPT over batches to produce structured events (title, deadline, sessions, eligibility, type).
3. **Stage 2 — personalise:** GPT scores each candidate against the student profile; returns the best matches.
4. **Filter:** Closed/past-deadline events are dropped via `event_filters.py`.

These mocks stand in for real LinkedIn/Xiaohongshu scraping; the extractor and matching logic are source-agnostic.

#### B. Email-derived events (digest only)

After inbox classification, `email_events.py` converts **urgent** and **relevant** inbox items that look event-like (keywords, detected timings) into the same event schema. They are **deduped** with feed events before conflict checking.

#### C. Calendar conflict check

`run_conflict_checks_batch()` loads the student’s Outlook calendar via Graph and assesses overlaps, same-day load, and deadline prep warnings (LLM-assisted with heuristic fallback).

Results are split into **urgent** (deadline within ~30 days) and **upcoming** in the digest.

### 3. Scholarship matching and drafting

#### Matching

Triggered by **`digest`** (stage 3) or **`scholarships`**.

1. **Cache:** Per-student `scholarship_cache` in Cosmos avoids re-running if the index watermark unchanged (`SCHOLARSHIP_CACHE_VERSION` + `index_scraped_at`).
2. **Stage 1 — Azure AI Search:** Structured OData filter (faculty, year, nationality, etc.) plus multiple text queries return a broad candidate set from the scraped index (~665 HKU scholarships + external schemes).
3. **Stage 2 — GPT eligibility:** Batches of candidates are reasoned against the full profile (GPA, programme, financial need, CV summary). Only **`qualifies: true`** and **`match_strength: strong`** survive.
4. **Package:** Matches split into **Apply Now** (open, deadline ≤30 days, low prep) vs **Prepare** (later deadlines or prep-heavy). Prototype **D.H. Chen (`ss_472`)** is injected when missing.

Index data is refreshed by timer functions in `function_app.py` (weekly full scrape, daily live-deadline updates).

#### Drafting / application

There are two paths:

| Path | Entry | Output |
|------|-------|--------|
| **Table-aware form fill** (primary for D.H. Chen) | **Start Application** on `ss_472` card → upload DOCX/PDF | Filled form via `agent/application/` |
| **Essay / question draft** (legacy) | **Start Draft** on other cards → upload form or paste questions | GPT answers via `agent/drafter.py` |

**Table-aware flow (`agent/application/`):**

1. Parse tables/paragraphs from DOCX or PDF.
2. GPT infers schema: simple fields, repeating lists (education, activities), long text, free fields.
3. Pre-fill from Cosmos profile; detect gaps.
4. Conversational interview collects missing list entries and long-text answers (with optional AI draft from GPA, interests, activities, CV).
5. Student reviews and approves → `docx_filler` / `pdf_filler` writes cells → upload to Azure Blob → SAS download link.

**Legacy draft flow:** Extract questions from an uploaded form, let the student pick items to draft, generate answers with profile + scholarship context.

---

## Project structure

```
hku-campus-agent/
├── bot_adapter.py              # Teams bot HTTP server (production entry)
├── app.py                        # Flask email demo (local dev only)
├── function_app.py               # Azure Functions timer scrapers
├── agent/
│   ├── handler.py                # Main bot logic
│   ├── graph.py                  # Microsoft Graph API
│   ├── profile.py                # Cosmos DB profiles
│   ├── email_pipeline.py         # Inbox triage pipeline
│   ├── classifier.py             # Email labelling
│   ├── matching.py               # Scholarship matching
│   ├── digest.py                 # Digest assembly
│   ├── drafter.py                # Legacy essay drafting
│   ├── application/              # Form parse, fill, interview
│   └── events/                   # Mock feeds, extraction, matching
├── scholarship_scraper/          # HKU scholar.aas.hku.hk scraper + indexer
├── live_deadlines_scraper/       # Live deadline updates
├── external_schemes_scraper/     # External scheme pages
├── copilot/                      # Teams app manifest & Adaptive Card JSON
├── tests/
└── scripts/
```

---

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment (Cosmos, OpenAI, Search, Bot, Graph OAuth, Storage)
cp .env.example .env   # if present; otherwise create .env from your Azure resources
```

**Key environment variables:**

| Variable | Purpose |
|----------|---------|
| `COSMOS_ENDPOINT`, `COSMOS_KEY`, `COSMOS_DATABASE` | Student profile storage |
| `AZURE_OPENAI_*` | GPT classification, matching, forms |
| `AZURE_SEARCH_*`, `SCHOLARSHIP_INDEX_NAME` | Scholarship index |
| `MicrosoftAppId`, `MicrosoftAppPassword`, `MicrosoftAppTenantId` | Bot Framework auth |
| `GRAPH_OAUTH_CONNECTION` | Bot OAuth connection name (default `GraphOAuth`) |
| `AZURE_STORAGE_CONNECTION_STRING` | Filled application uploads |
| `BOT_PUBLIC_URL` | Public HTTPS base for download links |

---

## Running

### Teams bot (local)

```bash
python bot_adapter.py
# Listens on PORT (default 3978) at POST /api/messages
```

Point your Azure Bot messaging endpoint at `https://<host>/api/messages`. Configure an OAuth connection with delegated **Mail.Read** and **Calendars.ReadWrite** scopes.

### Flask inbox demo

```bash
python app.py
# http://localhost:5000 — classifier UI with demo emails or GRAPH_ACCESS_TOKEN
```

### Scholarship scrapers

```bash
# Quick test (5 scholarships)
cd scholarship_scraper && python run_local.py --limit 5

# Full scrape + index (~15 min)
python run_local.py

# Validate scraper without Azure
python validate_test.py
```

### Tests

```bash
python -m pytest tests/
# or individual modules, e.g. python tests/test_scholarship_matching.py
```

---

## User commands

| Command | Action |
|---------|--------|
| `digest` | Full update: inbox → events → scholarships |
| `inbox` | Email triage only |
| `events` | Event feed matches (no inbox merge) |
| `scholarships` | Scholarship matches only |
| `help` | Command list |

After onboarding, upload a **CV** (PDF/DOCX) when prompted. Sign in with Microsoft when asked so inbox and calendar features work.

---

## Deployment

- **Bot:** GitHub Actions workflows under `.github/workflows/` deploy the bot and Azure Functions.
- **Copilot / Teams:** Package `copilot/manifest.json` and icons for sideloading or org catalog upload.
- **Scrapers:** Hosted as timer triggers in `function_app.py` on Azure Functions.

---

## License

HKU Campus Agent — internal / academic project. See repository for contribution and usage terms.
