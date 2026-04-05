# Trump Administration Litigation Tracker

A data pipeline and interactive visualization app that enriches the [Lawfare Trump Administration Litigation Tracker](https://www.lawfaremedia.org/projects-series/trials-of-the-trump-administration/tracking-trump-administration-litigation) with structured data from CourtListener's RECAP Archive.

## Live App

**[trump-litigation-tracker.streamlit.app](https://trump-litigation-tracker.streamlit.app/)**

## What It Does

The Lawfare tracker lists ~250 legal battles against the Trump administration. This project enriches that data by pulling party, attorney, and judge information from CourtListener for every docket, then visualizes the results.

### Key Numbers (as of last update)

| Metric | Count |
|--------|-------|
| Legal battles | 427 |
| Individual dockets | 637 |
| Appeal dockets | 179 |
| Distinct attorneys | 5,557 |
| Courts | 60 |

### Battles vs. Dockets

A single legal "battle" (e.g., *State of New York v. Trump* over the federal funding freeze) may spawn multiple court dockets: the original district court filing, a circuit court appeal, a cross-appeal, and sometimes a Supreme Court cert petition. The Lawfare tracker counts battles; the raw CSV lists every docket. This app tracks both and lets you toggle between them.

## Features

- **Overview Dashboard** — Battle/docket counts, executive action bar charts (toggle between battle-level and docket-level), court distribution, filing timeline
- **Attorney Network** — Top plaintiff-side and defendant-side (DOJ) attorneys by case count, organization/firm frequency chart
- **Judge Analysis** — Judge assignment counts, injunction and dismissal rates per judge
- **Case Explorer** — Searchable table with "All Dockets" / "Battles Only" toggle; click any case to see related dockets, parties, and attorneys
- **Executive Action Breakdown** — Cases grouped by executive action with status breakdown chart

## Data Sources

1. **Lawfare Litigation Tracker** — Seed data (case names, executive actions, statuses, summaries). Exported as CSV.
2. **CourtListener RECAP Archive** — Enrichment via REST API (parties, attorneys, firms, judges, courts, dates).

## Setup (Local Development)

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.template .env
```

Edit `.env` and add your CourtListener API token (get one at https://www.courtlistener.com/settings/):
```
COURTLISTENER_TOKEN=your_token_here
```

### 3. Seed Data

Place the Lawfare tracker CSV at `data/seed/lawfare_tracker.csv`. Expected columns:
- `Lawsuit` — HTML with case name and CourtListener link
- `Executive Action`, `Status`, `Summary`
- `Docket Number`, `Jurisdiction`, `Date Filed`, `Terminated`

## Running the Pipeline

Run each step sequentially. Steps 3 and 4 make API calls (~1 req/sec) and take ~10 minutes each for the full dataset. Steps 2-4 support an optional limit argument for pilot runs (e.g., `python src/03_enrich_parties.py 30`).

```bash
# Step 1: Load seed data into SQLite + assign battle IDs
python src/01_seed_loader.py

# Step 2: Match cases to CourtListener docket IDs (skip if CSV already has CL links)
python src/02_docket_matcher.py

# Step 3: Enrich with parties and attorneys (via search API)
python src/03_enrich_parties.py

# Step 4: Enrich with judge/court metadata (via dockets API)
python src/04_enrich_metadata.py

# Step 5: Compute analysis statistics
python src/05_analysis.py

# Step 6 (optional): Re-cluster battles on existing DB without re-running enrichment
python src/06_assign_battles.py
```

## Running the Visualization App

```bash
streamlit run app.py
# or: python3 -m streamlit run app.py
```

Opens at `http://localhost:8501`.

## Project Structure

```
data/
  seed/
    lawfare_tracker.csv          # Lawfare tracker export
  enriched/
    cases.db                     # SQLite database (all structured data)
    analysis.json                # Pre-computed stats for the dashboard
  raw/
    courtlistener_responses/     # Cached API responses (not in git)
src/
  01_seed_loader.py              # Parse CSV into SQLite, assign battle IDs
  02_docket_matcher.py           # Match case names to CourtListener docket IDs
  03_enrich_parties.py           # Pull parties & attorneys via search API
  04_enrich_metadata.py          # Pull judge, court, dates via dockets API
  05_analysis.py                 # Compute aggregate statistics
  06_assign_battles.py           # Cluster dockets into legal battles
  utils.py                       # Shared utilities (API, caching, normalization)
  org_lookup.py                  # Organization name normalization patterns
app.py                           # Streamlit visualization app
.env.template                    # Environment variable template
requirements.txt
```

## Database Schema

The SQLite database has three tables:

- **`cases`** — One row per docket (637 rows). Includes `battle_id` to cluster related dockets, `is_appeal` flag, `parent_docket` reference, judge, court, dates, executive action.
- **`parties`** — Parties per docket (plaintiffs, defendants, intervenors). ~14,000 rows.
- **`attorneys`** — Attorneys per docket with role (plaintiff_attorney / defendant_attorney) and organization/firm. ~11,000 rows.

## Technical Notes

- **Battle clustering**: Parses "Appeal of X:XX-cv-XXXXX" references from the Executive Action field to link appeals to parent filings, then groups by (normalized plaintiff + base executive action).
- **Attorney role classification**: DOJ/federal government firms → `defendant_attorney`; state AG offices → `plaintiff_attorney`; private firms → `plaintiff_attorney`. Based on firm name pattern matching since the search API returns flat (non-nested) party/attorney lists.
- **API strategy**: The CourtListener `/parties/` and `/attorneys/` endpoints require elevated access. We use the `/search/?q=docket_id:X&type=r` endpoint instead, which returns party, attorney, and firm data in search results.
- **Caching**: Every API response is cached to disk as JSON. The pipeline is fully re-runnable without re-fetching.
- **Rate limiting**: 1 request/second (well within CourtListener's 5,000/hour cap).
- **Organization normalization**: 50+ regex patterns map variations (e.g., "ACLU Foundation", "American Civil Liberties Union") to canonical names.

## Limitations

- ~26% of attorneys have no firm listed in CourtListener, so they're classified as generic "attorney" rather than plaintiff/defendant side.
- Intervenors and amici are classified as "plaintiff" since the search API doesn't provide per-party attorney mapping.
- 32 cases (of 637) lack CourtListener links in the CSV and aren't enriched.
- The 427 battle count is higher than Lawfare's ~254 because Lawfare groups more aggressively (e.g., multiple states suing over the same action = one Lawfare entry).
