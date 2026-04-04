#!/usr/bin/env python3
"""
Step 3: Enrich Parties

Pull parties and attorneys for each matched docket using the CourtListener
search API, which returns party, attorney, and firm data in search results.

The dedicated /parties/ and /attorneys/ API endpoints require elevated access,
so we use the search endpoint with docket_id filtering instead.
"""

import re
import sqlite3
from typing import Optional

from utils import DB_PATH, make_api_call, log_error, logger
from org_lookup import normalize_organization

# Government entity patterns for classifying plaintiff vs defendant
GOV_PATTERNS = [
    r"\btrump\b", r"\bbondi\b", r"\bnoem\b", r"\bhuffman\b",
    r"\bu\.?s\.?\s+dep", r"\bunited\s+states\b", r"\bdepartment\s+of\b",
    r"\boffice\s+of\b", r"\bdoj\b", r"\bdhs\b", r"\bopm\b", r"\bomb\b",
    r"\bimmigration\s+and\s+customs\b", r"\bice\b",
    r"\bsocial\s+security\b", r"\bfema\b", r"\bepa\b", r"\bnasa\b",
    r"\bdefense\s+health\b", r"\bsba\b", r"\bagency\b",
    r"\bsecretary\b", r"\badministrator\b",
    # DOJ-related firm patterns
    r"\bdoj-", r"\bu\.?s\.?\s+department\s+of\s+justice\b",
    r"\bunited\s+states\s+attorney",
]

# Known defendant-side (federal government) firm patterns
GOV_FIRM_PATTERNS = [
    r"\bdoj\b", r"\bu\.?s\.?\s*dep", r"\bunited\s+states\s+dep",
    r"\bunited\s+states\s+attorney", r"\busao\b",
    r"^doj-",  # DOJ divisions like "Doj-Usao"
]

# State AG / plaintiff-side government firm patterns
# State AGs are plaintiffs in these admin-challenge cases
STATE_AG_PATTERNS = [
    r"\battorney\s+general\b", r"\batt(orne)?y?\s+gen\b",
    r"\bdept?\s+of\s+att", r"\bag\s+office\b", r"\bag'?s\s+office\b",
    r"\boffice\s+of.*attorney\s+general\b",
]


def is_government_entity(name: str) -> bool:
    """Check if a party name looks like a government defendant."""
    name_lower = name.lower()
    for pat in GOV_PATTERNS:
        if re.search(pat, name_lower):
            return True
    return False


def is_gov_firm(firm: str) -> bool:
    """Check if a firm name looks like a government/DOJ firm."""
    firm_lower = firm.lower()
    for pat in GOV_FIRM_PATTERNS:
        if re.search(pat, firm_lower):
            return True
    return False


def get_matched_cases(conn: sqlite3.Connection) -> list[dict]:
    """Get cases that have been matched to CourtListener but lack party data."""
    cursor = conn.execute(
        """
        SELECT id, case_name, courtlistener_docket_id
        FROM cases
        WHERE courtlistener_docket_id IS NOT NULL
        AND id NOT IN (SELECT DISTINCT case_id FROM parties)
        ORDER BY id
        """
    )
    return [
        {"id": row[0], "case_name": row[1], "docket_id": row[2]}
        for row in cursor.fetchall()
    ]


def fetch_party_data_via_search(docket_id: int) -> Optional[dict]:
    """
    Fetch party, attorney, and firm data using the search API.
    Returns dict with 'party', 'attorney', 'firm' lists, or None.
    """
    result = make_api_call("search/", {
        "q": "docket_id:%d" % docket_id,
        "type": "r",
    })

    if not result or "results" not in result or not result["results"]:
        return None

    r = result["results"][0]
    return {
        "party": r.get("party", []),
        "attorney": r.get("attorney", []),
        "firm": r.get("firm", []),
        "jurisdictionType": r.get("jurisdictionType", ""),
    }


def classify_party_type(party_name: str, case_name: str) -> str:
    """
    Classify a party as plaintiff or defendant.

    Heuristics:
    - Government entities (Trump, DHS, DOJ, etc.) are defendants in these cases
    - The first name in "X v. Y" is the plaintiff side
    - Everything else defaults to plaintiff (since these are challenges TO gov)
    """
    if is_government_entity(party_name):
        return "defendant"

    # Check if party appears before or after "v." in case name
    parts = re.split(r"\s+v\.?\s+", case_name, flags=re.IGNORECASE, maxsplit=1)
    if len(parts) == 2:
        plaintiff_part = parts[0].lower()
        # Simple substring check
        name_words = party_name.lower().split()
        if name_words and any(w in plaintiff_part for w in name_words if len(w) > 3):
            return "plaintiff"

    return "plaintiff"  # default for non-gov in these admin-challenge cases


def is_state_ag_firm(firm: str) -> bool:
    """Check if a firm is a state attorney general's office."""
    firm_lower = firm.lower()
    for pat in STATE_AG_PATTERNS:
        if re.search(pat, firm_lower):
            return True
    return False


def classify_attorney_role(firm: str) -> str:
    """Classify attorney role based on their firm."""
    if not firm:
        return "attorney"
    if is_state_ag_firm(firm):
        return "plaintiff_attorney"  # State AGs are plaintiffs in these cases
    if is_gov_firm(firm):
        return "defendant_attorney"
    return "plaintiff_attorney"


def enrich_case(conn: sqlite3.Connection, case_id: int, case_name: str,
                docket_id: int) -> tuple[int, int]:
    """
    Enrich a single case with party and attorney data.
    Returns (parties_inserted, attorneys_inserted).
    """
    data = fetch_party_data_via_search(docket_id)
    if not data:
        return 0, 0

    parties_inserted = 0
    attorneys_inserted = 0

    # Insert parties
    for party_name in data["party"]:
        if not party_name:
            continue
        party_type = classify_party_type(party_name, case_name)
        org = normalize_organization(party_name) if is_government_entity(party_name) else None

        try:
            conn.execute(
                "INSERT INTO parties (case_id, name, party_type, organization) VALUES (?, ?, ?, ?)",
                (case_id, party_name, party_type, org),
            )
            parties_inserted += 1
        except Exception as e:
            log_error(e, f"Error inserting party: {party_name}")

    # Insert attorneys with their firms
    attorneys = data["attorney"]
    firms = data["firm"]

    for idx, att_name in enumerate(attorneys):
        if not att_name:
            continue

        # Match firm by index (CL returns parallel lists)
        firm = firms[idx] if idx < len(firms) else None
        role = classify_attorney_role(firm) if firm else "attorney"
        org = normalize_organization(firm) if firm else None

        try:
            conn.execute(
                "INSERT INTO attorneys (case_id, party_id, name, role, organization, address) VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, None, att_name, role, org, None),
            )
            attorneys_inserted += 1
        except Exception as e:
            log_error(e, f"Error inserting attorney: {att_name}")

    return parties_inserted, attorneys_inserted


def main(limit: int = 0):
    """Main entry point. If limit > 0, only process that many cases."""
    logger.info("=== Step 3: Enrich Parties ===")
    logger.info("Starting...")

    try:
        conn = sqlite3.connect(DB_PATH)

        cases = get_matched_cases(conn)
        total_available = len(cases)
        if limit > 0:
            cases = cases[:limit]
        total = len(cases)
        logger.info(f"Found {total_available} cases to enrich, processing {total}")

        if total == 0:
            logger.info("All cases already have parties!")
            conn.close()
            return 0

        enriched = 0

        for i, case in enumerate(cases, 1):
            case_id = case["id"]
            case_name = case["case_name"]
            docket_id = case["docket_id"]

            logger.info(f"[{i}/{total}] Enriching: {case_name} (docket {docket_id})")

            parties_inserted, attorneys_inserted = enrich_case(
                conn, case_id, case_name, docket_id
            )

            if parties_inserted > 0 or attorneys_inserted > 0:
                enriched += 1
                logger.info(f"  -> Inserted {parties_inserted} parties, {attorneys_inserted} attorneys")

            conn.commit()

        conn.close()

        logger.info("=== Summary ===")
        logger.info(f"Enriched: {enriched}/{total} cases")
        logger.info("Parties enrichment complete!")

        return 0

    except Exception as e:
        log_error(e, "Parties enrichment failed")
        return 1


if __name__ == "__main__":
    import sys
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    exit(main(limit=lim))
