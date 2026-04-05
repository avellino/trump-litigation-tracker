#!/usr/bin/env python3
"""
Step 7: Enrich Judge Appointer Data

For each judge in the database, look up which president appointed them
using CourtListener's People and Positions APIs.

Strategy:
  1. For each unique judge_name, try to find their CourtListener person record:
     a. Check if any of their dockets have an `assigned_to` URL (direct person link)
     b. Fallback: search People API by last name + first name
  2. Fetch the person's positions (judicial appointments)
  3. Follow the `appointer` field (a position URL) to get the president's name
  4. Store the appointing president in the `appointed_by` column of cases table

Usage:
    python src/07_enrich_appointers.py [limit]
"""

import re
import sqlite3
import sys
import time
from typing import Optional

from utils import (
    COURTLISTENER_BASE_URL,
    COURTLISTENER_TOKEN,
    DB_PATH,
    log_error,
    logger,
    make_api_call,
    get_cache_key,
    check_cache,
    save_cache,
)

# Cache president names by position URL to avoid redundant lookups
_president_cache: dict[str, Optional[str]] = {}


def parse_judge_name(judge_name: str) -> tuple[str, str, str]:
    """Parse a judge name into (first, middle, last).

    Handles formats like:
        'James E. Boasberg' -> ('James', 'E.', 'Boasberg')
        'Jia M. Cobb' -> ('Jia', 'M.', 'Cobb')
        'Rudolph Contreras' -> ('Rudolph', '', 'Contreras')
        'Colleen Kollar-Kotelly' -> ('Colleen', '', 'Kollar-Kotelly')
        'Gary S. Katzmann Timothy M. Reif Jane A. Restani' -> skip (multi-judge)
    """
    parts = judge_name.strip().split()
    if len(parts) < 2:
        return ("", "", judge_name.strip())
    if len(parts) == 2:
        return (parts[0], "", parts[1])
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    # More than 3 parts — could be multi-judge panel or long name
    # Try to detect multi-judge (multiple capitalized first names pattern)
    # For safety, use first and last
    return (parts[0], " ".join(parts[1:-1]), parts[-1])


def is_multi_judge(judge_name: str) -> bool:
    """Detect multi-judge panel names like 'Gary S. Katzmann Timothy M. Reif Jane A. Restani'."""
    parts = judge_name.strip().split()
    # Count how many parts look like first names (capitalized, no period)
    cap_no_period = [p for p in parts if p[0].isupper() and not p.endswith('.') and len(p) > 2]
    return len(cap_no_period) >= 4


def find_person_via_docket(conn: sqlite3.Connection, judge_name: str) -> Optional[str]:
    """Try to find a judge's person URL from their docket's assigned_to field."""
    # Get docket IDs for this judge
    rows = conn.execute(
        """SELECT courtlistener_docket_id FROM cases
           WHERE judge_name = ? AND courtlistener_docket_id IS NOT NULL
           LIMIT 5""",
        (judge_name,)
    ).fetchall()

    for (docket_id,) in rows:
        endpoint = f"dockets/{int(docket_id)}/"
        params = {"fields": "assigned_to,assigned_to_str"}
        data = make_api_call(endpoint, params)
        if data and data.get("assigned_to"):
            return data["assigned_to"]  # Full URL to person

    return None


def find_person_via_search(judge_name: str) -> Optional[dict]:
    """Search People API by name. Returns the person dict or None."""
    first, middle, last = parse_judge_name(judge_name)

    if not last:
        return None

    # Try with first + last name
    params = {"name_last": last}
    if first:
        params["name_first"] = first

    data = make_api_call("people/", params)
    if not data or not data.get("results"):
        # Try with just last name
        if first:
            data = make_api_call("people/", {"name_last": last})

    if not data or not data.get("results"):
        return None

    results = data["results"]

    # If only one result, use it
    if len(results) == 1:
        return results[0]

    # Multiple results — try to match by first name
    first_lower = first.lower() if first else ""
    for r in results:
        r_first = (r.get("name_first") or "").lower()
        if r_first == first_lower:
            return r
        # Partial match
        if first_lower and r_first.startswith(first_lower[:3]):
            return r

    # If we still can't match, return None rather than guess
    return None


def get_person_positions(person_url: str) -> list[dict]:
    """Fetch positions for a person. person_url can be full URL or just the person ID."""
    # Extract person ID from URL
    match = re.search(r'/people/(\d+)/', person_url)
    if not match:
        return []

    person_id = match.group(1)
    data = make_api_call(f"positions/", {"person": person_id})
    if not data or not data.get("results"):
        return []

    return data["results"]


def resolve_appointer(appointer_url: str) -> Optional[str]:
    """Follow an appointer position URL to get the president's name.

    The appointer field points to a Position (the president's appointment),
    which has a nested person object with the president's name.
    """
    if appointer_url in _president_cache:
        return _president_cache[appointer_url]

    # Extract position ID from URL
    match = re.search(r'/positions/(\d+)/', appointer_url)
    if not match:
        return None

    pos_id = match.group(1)
    data = make_api_call(f"positions/{pos_id}/")
    if not data:
        return None

    def _extract_name(pdata: dict) -> Optional[str]:
        first = pdata.get("name_first", "")
        middle = pdata.get("name_middle", "")
        last = pdata.get("name_last", "")
        parts = [p for p in [first, middle, last] if p]
        return " ".join(parts) if parts else None

    person = data.get("person", {})
    if isinstance(person, dict):
        name = _extract_name(person)
        _president_cache[appointer_url] = name
        return name
    elif isinstance(person, str):
        # person is a URL — need to fetch it
        pmatch = re.search(r'/people/(\d+)/', person)
        if pmatch:
            pdata = make_api_call(f"people/{pmatch.group(1)}/")
            if pdata:
                name = _extract_name(pdata)
                _president_cache[appointer_url] = name
                return name

    _president_cache[appointer_url] = None
    return None


def find_judicial_position(positions: list[dict], court_name: Optional[str] = None) -> Optional[dict]:
    """Find the federal judicial appointment position from a list of positions.

    Strategy:
    1. Prefer positions matching the court if provided
    2. Prefer federal judicial positions (position_type starts with "jud")
    3. Among federal positions, prefer the most recent one
    4. Skip state court positions (which have governor appointers)
    """
    # Known federal court URL patterns
    FEDERAL_COURT_PATTERNS = [
        "/courts/scotus/", "/courts/ca", "/courts/dcd/",
        # District courts: XX-d (e.g., nyd, cand, txwd)
    ]

    def is_likely_federal(pos: dict) -> bool:
        """Check if a position is likely a federal judicial appointment."""
        court = pos.get("court")
        if isinstance(court, dict):
            # Full court object — check jurisdiction field
            jurisdiction = (court.get("jurisdiction") or "").upper()
            # FD = Federal District, FB = Federal Bankruptcy, F = Federal Appellate
            # FS or empty for Supreme Court
            if jurisdiction in ("FD", "FB", "F"):
                return True
            # State courts have jurisdiction "S" or "ST" or "SA"
            if jurisdiction.startswith("S"):
                return False
            court_url = court.get("resource_uri", "")
            if "/courts/scotus/" in court_url:
                return True
            # Federal circuit courts: /courts/ca1/ through /courts/ca11/, /courts/cadc/, /courts/cafc/
            if re.search(r'/courts/ca\d+/', court_url) or "/courts/cadc/" in court_url or "/courts/cafc/" in court_url:
                return True
            return False
        elif isinstance(court, str):
            court_lower = court.lower()
            if "/courts/scotus/" in court_lower:
                return True
            if re.search(r'/courts/ca\d+/', court_lower) or "/courts/cadc/" in court_lower:
                return True
            if re.search(r'/courts/\w{2,4}d/', court_lower):
                return True
            return False
        # No court info — not conclusive
        return False

    # Categorize positions
    federal_with_appointer = []
    federal_no_appointer = []
    other_with_appointer = []

    for pos in positions:
        has_appointer = bool(pos.get("appointer"))
        is_fed = is_likely_federal(pos)

        if is_fed and has_appointer:
            federal_with_appointer.append(pos)
        elif is_fed:
            federal_no_appointer.append(pos)
        elif has_appointer:
            other_with_appointer.append(pos)

    # Priority: federal with appointer > other with appointer
    candidates = federal_with_appointer or other_with_appointer

    if not candidates:
        candidates = federal_no_appointer

    if not candidates:
        return None

    # If we have a court name, try to match
    if court_name and len(candidates) > 1:
        court_lower = court_name.lower()
        for pos in candidates:
            court = pos.get("court", "") or ""
            if isinstance(court, str) and court_lower in court.lower():
                return pos

    # Return most recent (last in list)
    return candidates[-1]


def lookup_judge_appointer(
    conn: sqlite3.Connection,
    judge_name: str,
    court_name: Optional[str] = None,
) -> Optional[str]:
    """Main function to look up which president appointed a judge.

    Returns president name string or None.
    """
    logger.info(f"Looking up appointer for: {judge_name}")

    # Strategy 1: Check docket's assigned_to field for direct person URL
    person_url = find_person_via_docket(conn, judge_name)

    if person_url:
        logger.info(f"  Found person via docket: {person_url}")
        positions = get_person_positions(person_url)
    else:
        # Strategy 2: Search People API
        person = find_person_via_search(judge_name)
        if not person:
            logger.warning(f"  Could not find person record for {judge_name}")
            return None

        person_id = person.get("id")
        person_url = f"https://www.courtlistener.com/api/rest/v4/people/{person_id}/"
        logger.info(f"  Found person via search: {person['name_first']} {person['name_last']} (id={person_id})")

        # Get positions from the person record if they're included
        pos_urls = person.get("positions", [])
        if pos_urls and isinstance(pos_urls[0], str):
            # Positions are URLs — need to fetch via positions endpoint
            positions = get_person_positions(person_url)
        elif pos_urls and isinstance(pos_urls[0], dict):
            positions = pos_urls
        else:
            positions = get_person_positions(person_url)

    if not positions:
        logger.warning(f"  No positions found for {judge_name}")
        return None

    # Find the judicial position
    jud_pos = find_judicial_position(positions, court_name)
    if not jud_pos:
        logger.warning(f"  No judicial position found for {judge_name}")
        return None

    # Get appointer
    appointer_url = jud_pos.get("appointer")
    if not appointer_url:
        logger.warning(f"  No appointer listed for {judge_name}")
        return None

    president = resolve_appointer(appointer_url)
    if president:
        logger.info(f"  Appointed by: {president}")
    else:
        logger.warning(f"  Could not resolve appointer URL: {appointer_url}")

    return president


def main():
    logger.info("=== Step 7: Enrich Judge Appointer Data ===")

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    conn = sqlite3.connect(DB_PATH)

    # Add appointed_by column if it doesn't exist
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
    if "appointed_by" not in existing_cols:
        conn.execute("ALTER TABLE cases ADD COLUMN appointed_by TEXT")
        conn.commit()
        logger.info("Added 'appointed_by' column to cases table")

    # Get distinct judges (skip multi-judge panels and already-resolved ones)
    if limit:
        judges = conn.execute("""
            SELECT judge_name, COUNT(*) as cnt, MIN(court) as sample_court
            FROM cases
            WHERE judge_name IS NOT NULL AND judge_name != ''
              AND (appointed_by IS NULL OR appointed_by = '')
            GROUP BY judge_name
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,)).fetchall()
    else:
        judges = conn.execute("""
            SELECT judge_name, COUNT(*) as cnt, MIN(court) as sample_court
            FROM cases
            WHERE judge_name IS NOT NULL AND judge_name != ''
              AND (appointed_by IS NULL OR appointed_by = '')
            GROUP BY judge_name
            ORDER BY cnt DESC
        """).fetchall()

    logger.info(f"Looking up appointers for {len(judges)} judges")

    found = 0
    not_found = 0
    skipped = 0

    for judge_name, case_count, court_name in judges:
        if is_multi_judge(judge_name):
            logger.info(f"Skipping multi-judge panel: {judge_name}")
            skipped += 1
            continue

        try:
            president = lookup_judge_appointer(conn, judge_name, court_name)

            if president:
                # Normalize common president name variations
                normalized = normalize_president_name(president)
                # Verify this is actually a US president (not a state governor)
                if is_known_president(normalized):
                    conn.execute(
                        "UPDATE cases SET appointed_by = ? WHERE judge_name = ?",
                        (normalized, judge_name)
                    )
                    conn.commit()
                    found += 1
                    logger.info(f"  [{found}/{len(judges)}] {judge_name} -> {normalized} ({case_count} cases)")
                else:
                    logger.warning(f"  Non-presidential appointer for {judge_name}: {president} (skipping)")
                    not_found += 1
                    continue
            else:
                not_found += 1
                logger.warning(f"  [{found}/{len(judges)}] {judge_name} -> NOT FOUND ({case_count} cases)")

        except Exception as e:
            log_error(e, f"Looking up {judge_name}")
            not_found += 1

    # Summary
    total = found + not_found + skipped
    logger.info(f"\n=== Summary ===")
    logger.info(f"Judges processed: {total}")
    logger.info(f"  Found appointer: {found}")
    logger.info(f"  Not found: {not_found}")
    logger.info(f"  Skipped (multi-judge): {skipped}")

    # Show coverage
    total_cases = conn.execute("SELECT COUNT(*) FROM cases WHERE judge_name IS NOT NULL AND judge_name != ''").fetchone()[0]
    covered = conn.execute("SELECT COUNT(*) FROM cases WHERE appointed_by IS NOT NULL AND appointed_by != ''").fetchone()[0]
    logger.info(f"\nCoverage: {covered}/{total_cases} cases have appointer data ({covered/total_cases*100:.1f}%)")

    # Show president distribution
    dist = conn.execute("""
        SELECT appointed_by, COUNT(*) as cnt
        FROM cases
        WHERE appointed_by IS NOT NULL AND appointed_by != ''
        GROUP BY appointed_by
        ORDER BY cnt DESC
    """).fetchall()
    if dist:
        logger.info("\nAppointments by president:")
        for president, count in dist:
            logger.info(f"  {president}: {count} cases")

    conn.close()
    return 0


KNOWN_PRESIDENTS = {
    "Barack Obama", "Donald Trump", "Joe Biden", "George W. Bush",
    "Bill Clinton", "Ronald Reagan", "George H.W. Bush", "Jimmy Carter",
    "Richard Nixon", "Lyndon B. Johnson", "Gerald Ford", "John F. Kennedy",
    "Dwight D. Eisenhower", "Harry S. Truman", "Franklin D. Roosevelt",
}


def is_known_president(name: str) -> bool:
    """Check if a normalized name is a known US president."""
    return name in KNOWN_PRESIDENTS


def normalize_president_name(name: str) -> str:
    """Normalize president names to consistent short forms."""
    name = name.strip()

    # Map full names (first middle last) from CourtListener to common names
    # resolve_appointer now returns "{first} {middle} {last}"
    mappings = {
        "barack hussein obama": "Barack Obama",
        "barack obama": "Barack Obama",
        "donald john trump": "Donald Trump",
        "donald trump": "Donald Trump",
        "joseph robinette biden": "Joe Biden",
        "joseph biden": "Joe Biden",
        "george w. bush": "George W. Bush",
        "george walker bush": "George W. Bush",
        "george bush": "George W. Bush",
        "george h.w. bush": "George H.W. Bush",
        "george herbert walker bush": "George H.W. Bush",
        "william jefferson clinton": "Bill Clinton",
        "william clinton": "Bill Clinton",
        "ronald wilson reagan": "Ronald Reagan",
        "ronald reagan": "Ronald Reagan",
        "james earl carter": "Jimmy Carter",
        "james carter": "Jimmy Carter",
        "richard milhous nixon": "Richard Nixon",
        "richard nixon": "Richard Nixon",
        "lyndon baines johnson": "Lyndon B. Johnson",
        "lyndon johnson": "Lyndon B. Johnson",
        "gerald rudolph ford": "Gerald Ford",
        "gerald ford": "Gerald Ford",
        "john fitzgerald kennedy": "John F. Kennedy",
        "john kennedy": "John F. Kennedy",
        "dwight david eisenhower": "Dwight D. Eisenhower",
        "dwight eisenhower": "Dwight D. Eisenhower",
        "harry s. truman": "Harry S. Truman",
        "harry truman": "Harry S. Truman",
    }

    lower = name.lower().strip()

    # Check exact matches
    if lower in mappings:
        return mappings[lower]

    # Partial match (e.g. extra suffixes like "II" or "Jr.")
    for key, val in mappings.items():
        if key in lower or lower in key:
            return val

    # If not in mappings, return as-is
    return name


if __name__ == "__main__":
    exit(main())
