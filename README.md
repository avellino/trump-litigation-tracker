# Trump Administration Litigation Tracker

A data pipeline and interactive visualization app that enriches the Lawfare Trump Administration Litigation Tracker with structured data from CourtListener's RECAP Archive.

## Features

- **Data Pipeline**: Extracts and enriches litigation data from CourtListener
- **Visualization App**: Interactive Streamlit dashboard with 5 views
- **Attorney Networks**: Track which attorneys appear in multiple cases
- **Organization Frequency**: See which firms/orgs are most active
- **Judge Analysis**: View judge assignments and outcomes
- **Case Explorer**: Search and browse all cases with details

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy the template and add your CourtListener API token:

```bash
cp .env.template .env
```

Edit `.env` and add your token:
```
COURTLISTENER_TOKEN=your_actual_token_here
```

Get your token from: https://www.courtlistener.com/settings/

### 3. Add Seed Data

Place the Lawfare tracker CSV at `data/seed/lawfare_tracker.csv` with columns:
- `Lawsuit` (HTML with case name and CourtListener link)
- `Executive Action`
- `Status`
- `Summary`
- `Docket Number`, `Jurisdiction`, `Date Filed`, `Terminated` (optional but used if present)

## Running the Pipeline

Run each step sequentially:

```bash
# Step 1: Load seed data into SQLite
python src/01_seed_loader.py

# Step 2: Match cases to CourtListener docket IDs
python src/02_docket_matcher.py

# Step 3: Enrich with parties and attorneys
python src/03_enrich_parties.py

# Step 4: Enrich with judge/court metadata
python src/04_enrich_metadata.py

# Step 5: Compute analysis statistics
python src/05_analysis.py
```

## Running the Visualization App

```bash
streamlit run app.py
# or: python3 -m streamlit run app.py
```

The app will open at `http://localhost:8501` with 5 pages:

1. **Overview Dashboard** - Metrics, charts, and timeline
2. **Attorney Network** - Top attorneys and organizations
3. **Judge Analysis** - Judge assignments and outcomes
4. **Case Explorer** - Searchable case table with details
5. **Executive Action Breakdown** - Cases grouped by executive action

## Project Structure

```
data/
  seed/
    lawfare_tracker.csv      # User-provided seed data
  enriched/
    cases.db                 # SQLite database
  raw/
    courtlistener_responses/ # Cached API responses
src/
  01_seed_loader.py          # Load CSV into SQLite
  02_docket_matcher.py       # Match to CourtListener
  03_enrich_parties.py       # Extract parties/attorneys
  04_enrich_metadata.py      # Extract judge/court info
  05_analysis.py             # Compute statistics
  utils.py                   # Shared utilities
  org_lookup.py              # Organization normalization
app.py                       # Streamlit app
.env                         # API token (not in git)
requirements.txt
README.md
```

## Notes

- The pipeline is re-runnable; cached API responses prevent duplicate requests
- Unmatched cases are logged to `data/unmatched_cases.txt`
- Errors are logged to `data/pipeline_errors.log`
- Rate limit: CourtListener allows 5,000 requests/hour
