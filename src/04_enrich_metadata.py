#!/usr/bin/env python3
"""
Step 4: Enrich Metadata

Pull judge, court, dates, and other metadata for each matched docket.
Updates the cases table with enriched information.
"""

import sqlite3
from datetime import datetime
from typing import Optional

from utils import DB_PATH, make_api_call, log_error, logger


def get_enrichable_cases(conn: sqlite3.Connection) -> list[dict]:
    """Get cases that have docket_id but missing metadata."""
    cursor = conn.execute(
        """
        SELECT id, case_name, courtlistener_docket_id
        FROM cases
        WHERE courtlistener_docket_id IS NOT NULL
        AND (court_type IS NULL OR judge_name IS NULL OR judge_name = '')
        ORDER BY id
        """
    )
    return [
        {"id": row[0], "case_name": row[1], "docket_id": row[2]}
        for row in cursor.fetchall()
    ]


def fetch_docket_metadata(docket_id: int) -> Optional[dict]:
    """Fetch docket metadata from CourtListener API."""
    result = make_api_call(f"dockets/{docket_id}/")
    return result if result else None


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """Parse date string to YYYY-MM-DD format."""
    if not date_str:
        return None

    date_str = date_str.strip()

    # Try ISO format first (most common from API)
    formats_full = [
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats_full:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Fallback: extract date part from ISO timestamp
    if "T" in date_str:
        return date_str.split("T")[0]

    # Try human-readable formats (unlikely from API but just in case)
    for fmt in ["%B %d, %Y", "%m/%d/%Y"]:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return date_str[:10] if len(date_str) >= 10 else None


def parse_court_type(court_name: Optional[str]) -> Optional[str]:
    """Determine court type from court name string."""
    if not court_name:
        return None

    court_lower = court_name.lower()

    if "supreme" in court_lower:
        return "supreme"
    elif "circuit" in court_lower or "court of appeals" in court_lower:
        return "circuit"
    elif any(x in court_lower for x in [
        "district", "d.", "s.d.", "n.d.", "e.d.", "m.d.", "c.d.", "w.d."
    ]):
        return "district"
    elif "bankruptcy" in court_lower:
        return "bankruptcy"

    return "unknown"


def update_case_metadata(
    conn: sqlite3.Connection, case_id: int, metadata: dict
) -> None:
    """Update case with metadata from CourtListener."""
    # CourtListener returns court as a URL or short ID — use court_id or
    # the human-readable court name from the nested object
    court = metadata.get("court_citation_string") or metadata.get("court", "")
    court_type = parse_court_type(court)

    # CourtListener uses assigned_to_str for the judge's name as a string.
    # assigned_to is a URL reference to the person resource.
    judge_name = (
        metadata.get("assigned_to_str")
        or metadata.get("referred_to_str")
        or ""
    )

    date_filed = parse_date(metadata.get("date_filed"))
    date_terminated = parse_date(metadata.get("date_terminated"))
    docket_number = metadata.get("docket_number", "")

    conn.execute(
        """
        UPDATE cases
        SET court = COALESCE(?, court),
            court_type = COALESCE(?, court_type),
            judge_name = COALESCE(NULLIF(?, ''), judge_name),
            date_filed = COALESCE(?, date_filed),
            date_terminated = COALESCE(?, date_terminated),
            docket_number = COALESCE(NULLIF(?, ''), docket_number)
        WHERE id = ?
        """,
        (court, court_type, judge_name, date_filed, date_terminated, docket_number, case_id),
    )


def main(limit: int = 0):
    """Main entry point. If limit > 0, only process that many cases."""
    logger.info("=== Step 4: Enrich Metadata ===")
    logger.info("Starting...")

    try:
        conn = sqlite3.connect(DB_PATH)

        cases = get_enrichable_cases(conn)
        total_available = len(cases)
        if limit > 0:
            cases = cases[:limit]
        total = len(cases)
        logger.info(f"Found {total_available} cases to enrich, processing {total}")

        if total == 0:
            logger.info("All cases already have metadata!")
            conn.close()
            return 0

        enriched = 0
        failed = 0

        for i, case in enumerate(cases, 1):
            case_id = case["id"]
            case_name = case["case_name"]
            docket_id = case["docket_id"]

            logger.info(f"[{i}/{total}] Enriching: {case_name} (docket {docket_id})")

            metadata = fetch_docket_metadata(docket_id)

            if metadata:
                update_case_metadata(conn, case_id, metadata)
                enriched += 1

                court = metadata.get("court_citation_string", "")
                judge = metadata.get("assigned_to_str", "")
                if court or judge:
                    details = []
                    if court:
                        details.append(f"court: {court}")
                    if judge:
                        details.append(f"judge: {judge}")
                    logger.info(f"  -> {', '.join(details)}")
            else:
                failed += 1
                logger.info(f"  -> No metadata found")

        conn.commit()
        conn.close()

        logger.info("=== Summary ===")
        logger.info(f"Enriched: {enriched}/{total} cases")
        logger.info(f"Failed: {failed}/{total} cases")
        logger.info("Metadata enrichment complete!")

        return 0

    except Exception as e:
        log_error(e, "Metadata enrichment failed")
        return 1


if __name__ == "__main__":
    import sys
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    exit(main(limit=lim))
