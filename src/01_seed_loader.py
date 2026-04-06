#!/usr/bin/env python3
"""
Step 1: Seed Loader

Load the Lawfare tracker CSV into SQLite database.
Creates the database schema and populates the cases table.
"""

import re
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from utils import DB_PATH, SEED_DIR, normalize_case_name, log_error, logger

# Database schema
SCHEMA_SQL = """
-- Main cases table
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_name TEXT NOT NULL,
    normalized_case_name TEXT,
    docket_number TEXT,
    courtlistener_docket_id INTEGER,
    courtlistener_url TEXT,
    court TEXT,
    court_type TEXT,
    judge_name TEXT,
    executive_action TEXT,
    base_executive_action TEXT,  -- action without "Appeal of ..." suffix
    status TEXT,
    date_filed DATE,
    date_terminated DATE,
    summary TEXT,
    battle_id INTEGER,           -- clusters related dockets into one "legal battle"
    is_appeal INTEGER DEFAULT 0, -- 1 if this docket is an appeal of another
    parent_docket TEXT,          -- docket number this row is an appeal of
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Parties table
CREATE TABLE IF NOT EXISTS parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    party_type TEXT,
    organization TEXT,
    FOREIGN KEY (case_id) REFERENCES cases(id)
);

-- Attorneys table
CREATE TABLE IF NOT EXISTS attorneys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL,
    party_id INTEGER,
    name TEXT NOT NULL,
    role TEXT,
    organization TEXT,
    address TEXT,
    FOREIGN KEY (case_id) REFERENCES cases(id),
    FOREIGN KEY (party_id) REFERENCES parties(id)
);

-- Index for faster lookups
CREATE INDEX IF NOT EXISTS idx_cases_normalized ON cases(normalized_case_name);
CREATE INDEX IF NOT EXISTS idx_cases_cl_docket ON cases(courtlistener_docket_id);
CREATE INDEX IF NOT EXISTS idx_cases_battle ON cases(battle_id);
CREATE INDEX IF NOT EXISTS idx_parties_case ON parties(case_id);
CREATE INDEX IF NOT EXISTS idx_attorneys_case ON attorneys(case_id);
"""

# Views for analysis
VIEWS_SQL = """
-- Top attorneys by case count
CREATE VIEW IF NOT EXISTS attorney_case_count AS
SELECT
    name,
    organization,
    role,
    COUNT(DISTINCT case_id) as case_count
FROM attorneys
GROUP BY name, organization, role
ORDER BY case_count DESC;

-- Court distribution with injunction counts
CREATE VIEW IF NOT EXISTS court_distribution AS
SELECT
    court,
    COUNT(*) as case_count,
    SUM(CASE WHEN status LIKE '%injunction%' OR status LIKE '%TRO%' THEN 1 ELSE 0 END) as injunctions
FROM cases
WHERE court IS NOT NULL
GROUP BY court
ORDER BY case_count DESC;

-- Organization frequency
CREATE VIEW IF NOT EXISTS org_frequency AS
SELECT
    organization,
    role,
    COUNT(DISTINCT case_id) as case_count
FROM attorneys
GROUP BY organization, role
ORDER BY case_count DESC;
"""


def extract_case_name_from_html(html_str: str) -> str:
    """Extract case name from HTML like '<a href=...> <i> Case Name </i> </a>'."""
    if not html_str or not isinstance(html_str, str):
        return str(html_str) if html_str else ""

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", "", html_str)
    return text.strip()


def extract_courtlistener_docket_id(html_str: str) -> Optional[int]:
    """Extract CourtListener docket ID from the Lawsuit column HTML link."""
    if not html_str or not isinstance(html_str, str):
        return None

    match = re.search(r"courtlistener\.com/docket/(\d+)/", html_str)
    if match:
        return int(match.group(1))
    return None


def extract_courtlistener_url(html_str: str) -> Optional[str]:
    """Extract the full CourtListener or Supreme Court URL from the Lawsuit column HTML link."""
    if not html_str or not isinstance(html_str, str):
        return None

    # Try CourtListener URL first
    match = re.search(r"(https?://www\.courtlistener\.com/docket/\d+/[^\"'\s>]*)", html_str)
    if match:
        return match.group(1)

    # Try Supreme Court URL
    match = re.search(r"(https?://www\.supremecourt\.gov/[^\"'\s>]*)", html_str)
    if match:
        return match.group(1)

    return None


def strip_html(text: str) -> str:
    """Strip HTML tags from a string."""
    if not text or not isinstance(text, str):
        return str(text) if text else ""
    return re.sub(r"<[^>]+>", "", text).strip()


def parse_date(date_str: str) -> Optional[str]:
    """Parse various date formats from the CSV to YYYY-MM-DD."""
    if not date_str or not isinstance(date_str, str) or date_str.strip() == "":
        return None

    date_str = date_str.strip()

    # Handle "Terminated YYYY-MM-DD" prefix
    if date_str.lower().startswith("terminated"):
        date_str = date_str.split(None, 1)[-1].strip()

    # Handle "Active" or other non-date status values
    if date_str.lower() in ("active", ""):
        return None

    from datetime import datetime

    formats = [
        "%Y-%m-%d",
        "%B %d, %Y",      # "May 7, 2025"
        "%b %d, %Y",      # "May 7, 2025" (short month)
        "%b. %d, %Y",     # "Dec. 17, 2024"
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def parse_appeal_info(executive_action: str) -> tuple[str, int, Optional[str]]:
    """
    Parse the executive action field to detect appeals.

    Returns: (base_action, is_appeal, parent_docket)
    Examples:
        "Birthright Citizenship  Appeal of 2:25-cv-00127"
        -> ("Birthright Citizenship", 1, "2:25-cv-00127")

        "Federal Funding Freeze"
        -> ("Federal Funding Freeze", 0, None)
    """
    if not executive_action:
        return ("", 0, None)

    # Match "Appeal of <docket_number>" at the end
    # Docket formats: "1:25-cv-00039", "25-1138", "25-5046", etc.
    match = re.search(
        r"(.+?)\s+Appeal\s+of\s+([\d:]+[-\w,;\s]+\d+)",
        executive_action,
        re.IGNORECASE,
    )
    if match:
        base_action = match.group(1).strip().rstrip(",;")
        parent_ref = match.group(2).strip()
        # Take just the first docket number if there are multiple
        first_docket = re.match(r"([\d:]+-[\w]+-[\d]+|\d+-\d+)", parent_ref)
        parent_docket = first_docket.group(1) if first_docket else parent_ref
        return (base_action, 1, parent_docket)

    return (executive_action.strip(), 0, None)


def normalize_plaintiff(case_name: str) -> str:
    """
    Extract and normalize the plaintiff name for battle clustering.
    """
    # Split on "v." / "v " / "vs."
    parts = re.split(r"\s+v\.?\s+", case_name, maxsplit=1, flags=re.IGNORECASE)
    plaintiff = parts[0].strip().lower()

    # Handle consolidated cases with ";" — take the first
    if ";" in plaintiff:
        plaintiff = plaintiff.split(";")[0].strip()

    # Normalize common variations
    plaintiff = re.sub(r"^state of\s+", "", plaintiff)
    plaintiff = re.sub(r"^commonwealth of\s+", "", plaintiff)
    plaintiff = re.sub(r",?\s*(inc|llc|et al|pllc|l\.?l\.?c)\.?.*$", "", plaintiff)
    plaintiff = re.sub(r"\s+", " ", plaintiff).strip()

    return plaintiff


def assign_battle_ids(conn: sqlite3.Connection) -> int:
    """
    Cluster dockets into legal battles.

    Strategy:
    1. Link appeals to their parent docket via parent_docket field.
    2. Group remaining dockets by (base_executive_action, normalized_plaintiff).
    3. Each group gets a unique battle_id.

    Returns the number of distinct battles.
    """
    logger.info("Assigning battle IDs...")

    # Fetch all cases
    rows = conn.execute(
        """
        SELECT id, case_name, docket_number, base_executive_action,
               is_appeal, parent_docket
        FROM cases ORDER BY id
        """
    ).fetchall()

    # Build lookup: docket_number -> case id(s)
    docket_to_ids = {}
    for row in rows:
        case_id, case_name, docket_num, base_action, is_appeal, parent_docket = row
        if docket_num:
            docket_to_ids.setdefault(docket_num, []).append(case_id)

    # Phase 1: Build parent links for appeals
    # appeal_case_id -> parent_case_id
    parent_map = {}
    for row in rows:
        case_id, case_name, docket_num, base_action, is_appeal, parent_docket = row
        if is_appeal and parent_docket:
            parent_ids = docket_to_ids.get(parent_docket, [])
            if parent_ids:
                parent_map[case_id] = parent_ids[0]

    # Phase 2: Group non-appeal cases by (base_action, plaintiff)
    # Then assign appeals to their parent's group
    group_key_to_ids = {}
    case_to_group_key = {}

    for row in rows:
        case_id, case_name, docket_num, base_action, is_appeal, parent_docket = row

        if case_id in parent_map:
            # This is an appeal — will be assigned to parent's group later
            continue

        plaintiff = normalize_plaintiff(case_name)
        action = (base_action or "").strip().lower()
        group_key = (action, plaintiff)

        group_key_to_ids.setdefault(group_key, []).append(case_id)
        case_to_group_key[case_id] = group_key

    # Phase 3: Assign appeals to parent's group
    for appeal_id, parent_id in parent_map.items():
        if parent_id in case_to_group_key:
            gk = case_to_group_key[parent_id]
            group_key_to_ids[gk].append(appeal_id)
            case_to_group_key[appeal_id] = gk
        else:
            # Parent itself is an appeal or wasn't found — make its own group
            row_data = next(r for r in rows if r[0] == appeal_id)
            plaintiff = normalize_plaintiff(row_data[1])
            action = (row_data[3] or "").strip().lower()
            gk = (action, plaintiff)
            group_key_to_ids.setdefault(gk, []).append(appeal_id)
            case_to_group_key[appeal_id] = gk

    # Phase 4: Assign sequential battle_ids
    battle_id = 0
    total_assigned = 0
    for group_key, case_ids in group_key_to_ids.items():
        battle_id += 1
        for cid in case_ids:
            conn.execute(
                "UPDATE cases SET battle_id = ? WHERE id = ?",
                (battle_id, cid),
            )
            total_assigned += 1

    conn.commit()

    logger.info(f"Assigned {total_assigned} dockets to {battle_id} battles")
    return battle_id


def create_database() -> sqlite3.Connection:
    """Create the SQLite database and schema."""
    # Remove existing database for clean start
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    # executescript() handles multiple SQL statements separated by semicolons
    conn.executescript(SCHEMA_SQL)
    conn.executescript(VIEWS_SQL)
    logger.info(f"Created database at {DB_PATH}")
    return conn


def load_seed_data(conn: sqlite3.Connection) -> int:
    """Load seed data from CSV into the database."""
    csv_path = SEED_DIR / "lawfare_tracker.csv"

    if not csv_path.exists():
        logger.error(f"Seed file not found: {csv_path}")
        logger.info("Creating sample data for testing...")
        return load_sample_data(conn)

    df = pd.read_csv(csv_path)
    logger.info(f"Loaded {len(df)} rows from {csv_path}")
    logger.info(f"CSV columns: {list(df.columns)}")

    # Map actual CSV columns to our schema
    # CSV columns: Lawsuit, Executive Action, Status, Last Updated, Summary,
    #              Docket Number, Jurisdiction, Date Filed, Terminated

    if "Lawsuit" not in df.columns:
        logger.error(f"Expected 'Lawsuit' column in CSV. Found: {list(df.columns)}")
        return 0

    inserted = 0
    for _, row in df.iterrows():
        try:
            lawsuit_html = str(row.get("Lawsuit", ""))
            case_name = extract_case_name_from_html(lawsuit_html)
            if not case_name:
                continue

            normalized = normalize_case_name(case_name)
            cl_docket_id = extract_courtlistener_docket_id(lawsuit_html)
            cl_url = extract_courtlistener_url(lawsuit_html)
            executive_action = strip_html(str(row.get("Executive Action", "") or ""))
            status = str(row.get("Status", "") or "").strip()
            summary = str(row.get("Summary", "") or "").strip()
            docket_number = str(row.get("Docket Number", "") or "").strip()
            jurisdiction = str(row.get("Jurisdiction", "") or "").strip()
            date_filed = parse_date(str(row.get("Date Filed", "") or ""))
            terminated_raw = str(row.get("Terminated", "") or "").strip()
            date_terminated = parse_date(terminated_raw)

            # Parse appeal info from executive_action field
            base_action, is_appeal, parent_docket = parse_appeal_info(executive_action)

            conn.execute(
                """
                INSERT INTO cases (
                    case_name, normalized_case_name, courtlistener_docket_id,
                    courtlistener_url,
                    docket_number, court, executive_action, base_executive_action,
                    status, summary, date_filed, date_terminated,
                    is_appeal, parent_docket
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_name, normalized, cl_docket_id,
                    cl_url,
                    docket_number or None, jurisdiction or None,
                    executive_action, base_action, status, summary,
                    date_filed, date_terminated,
                    is_appeal, parent_docket,
                ),
            )
            inserted += 1

        except Exception as e:
            log_error(e, f"Error inserting case: {row.get('Lawsuit', 'unknown')}")

    conn.commit()
    logger.info(f"Inserted {inserted} cases into database")

    # Report how many already have CourtListener docket IDs from the CSV links
    cursor = conn.execute(
        "SELECT COUNT(*) FROM cases WHERE courtlistener_docket_id IS NOT NULL"
    )
    linked = cursor.fetchone()[0]
    logger.info(f"{linked}/{inserted} cases already have CourtListener docket IDs from CSV links")

    return inserted


def load_sample_data(conn: sqlite3.Connection) -> int:
    """Load sample pilot cases for testing."""
    sample_cases = [
        {
            "case_name": "J.G.G. v. Trump",
            "normalized_case_name": "jgg trump",
            "courtlistener_docket_id": 69741724,
            "executive_action": "Alien Enemies Act",
            "status": "Active - Injunction Pending",
            "summary": "Challenge to use of Alien Enemies Act for deportations",
        },
        {
            "case_name": "G.F.F. v. Trump",
            "normalized_case_name": "gff trump",
            "courtlistener_docket_id": 69857769,
            "executive_action": "Alien Enemies Act",
            "status": "Active",
            "summary": "Parallel challenge to Alien Enemies Act deportations",
        },
        {
            "case_name": "Barbara v. Trump",
            "normalized_case_name": "barbara trump",
            "executive_action": "Birthright Citizenship EO",
            "status": "Active - TRO Granted",
            "summary": "Challenge to executive order on birthright citizenship",
        },
        {
            "case_name": "State of New York v. Trump",
            "normalized_case_name": "state of new york trump",
            "executive_action": "Various",
            "status": "Active",
            "summary": "State challenge to multiple executive actions",
        },
        {
            "case_name": "League of Women Voters v. Trump",
            "normalized_case_name": "league of women voters trump",
            "executive_action": "Voting Rights EO",
            "status": "Active",
            "summary": "Challenge to voting rights executive order",
        },
        {
            "case_name": "Orr v. Trump",
            "normalized_case_name": "orr trump",
            "executive_action": "Transgender Passport Policy",
            "status": "Active",
            "summary": "Challenge to transgender passport designation policy",
        },
    ]

    for case in sample_cases:
        try:
            conn.execute(
                """
                INSERT INTO cases (
                    case_name, normalized_case_name, courtlistener_docket_id,
                    executive_action, status, summary
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    case["case_name"],
                    case["normalized_case_name"],
                    case.get("courtlistener_docket_id"),
                    case["executive_action"],
                    case["status"],
                    case["summary"],
                ),
            )
        except Exception as e:
            log_error(e, f"Error inserting sample case: {case['case_name']}")

    conn.commit()
    logger.info(f"Inserted {len(sample_cases)} sample cases for testing")
    return len(sample_cases)


def main():
    """Main entry point."""
    logger.info("=== Step 1: Seed Loader ===")
    logger.info("Starting...")

    try:
        conn = create_database()
        count = load_seed_data(conn)

        if count == 0:
            logger.error("No cases loaded. Check your data file or try again.")
            conn.close()
            return 1

        # Assign battle IDs (cluster related dockets)
        num_battles = assign_battle_ids(conn)

        # Print summary
        cursor = conn.execute("SELECT COUNT(*) FROM cases")
        total = cursor.fetchone()[0]
        logger.info(f"Database contains {total} dockets in {num_battles} legal battles")

        conn.close()
        logger.info("Seed loader complete!")
        return 0

    except Exception as e:
        log_error(e, "Seed loader failed")
        return 1


if __name__ == "__main__":
    exit(main())
