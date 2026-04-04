#!/usr/bin/env python3
"""
Step 2: Docket Matcher

Match case names to CourtListener docket IDs using the search API.
Uses fuzzy matching to find the best docket for each case.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from thefuzz import fuzz

from utils import (
    DB_PATH,
    DATA_DIR,
    make_api_call,
    normalize_case_name,
    log_error,
    logger,
)

# Minimum fuzzy match score to accept a match
MIN_MATCH_SCORE = 70


def get_unmatched_cases(conn: sqlite3.Connection) -> list[dict]:
    """Get cases that haven't been matched to CourtListener yet."""
    cursor = conn.execute(
        """
        SELECT id, case_name, normalized_case_name
        FROM cases
        WHERE courtlistener_docket_id IS NULL
        ORDER BY id
        """
    )
    return [
        {"id": row[0], "case_name": row[1], "normalized": row[2]}
        for row in cursor.fetchall()
    ]


def search_courtlistener(case_name: str) -> Optional[dict]:
    """
    Search CourtListener for a case name.

    Returns the best matching docket or None.
    """
    # Try with full case name first
    params = {
        "q": case_name,
        "type": "r",  # RECAP filings
        "filed_after": "2025-01-20",  # Since Trump took office
    }

    result = make_api_call("search/", params)

    if not result or "results" not in result:
        return None

    return result


def extract_docket_info(search_result: dict) -> list[dict]:
    """
    Extract docket information from search results.

    Returns list of potential docket matches.
    """
    dockets = []

    if "results" not in search_result:
        return dockets

    for item in search_result["results"]:
        # Get docket_id and case_name from search result
        docket_id = item.get("docket_id")
        case_name = item.get("case_name", "")

        if docket_id:
            dockets.append(
                {
                    "docket_id": docket_id,
                    "case_name": case_name,
                    "source_type": item.get("source_type", ""),
                }
            )

    return dockets


def find_best_match(
    query_case_name: str, dockets: list[dict]
) -> Optional[dict]:
    """
    Find the best matching docket using fuzzy string matching.

    Returns the best match or None if no good match found.
    """
    if not dockets:
        return None

    best_match = None
    best_score = 0

    query_normalized = normalize_case_name(query_case_name)

    for docket in dockets:
        docket_name = docket.get("case_name", "")
        docket_normalized = normalize_case_name(docket_name)

        # Try multiple matching strategies
        scores = [
            fuzz.ratio(query_normalized, docket_normalized),
            fuzz.partial_ratio(query_normalized, docket_normalized),
            fuzz.token_sort_ratio(query_normalized, docket_normalized),
        ]

        score = max(scores)

        if score > best_score:
            best_score = score
            best_match = docket

    if best_score >= MIN_MATCH_SCORE and best_match:
        return best_match

    return None


def log_unmatched(case: dict, reason: str) -> None:
    """Log an unmatched case to file."""
    unmatched_file = DATA_DIR / "unmatched_cases.txt"
    with open(unmatched_file, "a") as f:
        f.write(f"{case['case_name']} - {reason}\n")


def main():
    """Main entry point."""
    logger.info("=== Step 2: Docket Matcher ===")
    logger.info("Starting...")

    try:
        conn = sqlite3.connect(DB_PATH)

        # Get unmatched cases
        cases = get_unmatched_cases(conn)
        total = len(cases)
        logger.info(f"Found {total} cases to match")

        if total == 0:
            logger.info("All cases already matched!")
            conn.close()
            return 0

        matched = 0
        unmatched = 0

        for i, case in enumerate(cases, 1):
            case_id = case["id"]
            case_name = case["case_name"]

            logger.info(f"[{i}/{total}] Matching: {case_name}")

            # Search CourtListener
            search_result = search_courtlistener(case_name)

            if not search_result:
                log_unmatched(case, "No search results")
                unmatched += 1
                continue

            # Extract docket info
            dockets = extract_docket_info(search_result)

            if not dockets:
                log_unmatched(case, "No dockets found in results")
                unmatched += 1
                continue

            # Find best match
            best_match = find_best_match(case_name, dockets)

            if best_match:
                # Update database with docket ID
                conn.execute(
                    "UPDATE cases SET courtlistener_docket_id = ? WHERE id = ?",
                    (best_match["docket_id"], case_id),
                )
                matched += 1
                logger.info(f"  -> Matched to docket {best_match['docket_id']}")
            else:
                log_unmatched(case, f"No match above threshold ({MIN_MATCH_SCORE})")
                unmatched += 1

        conn.commit()
        conn.close()

        # Print summary
        logger.info("=== Summary ===")
        logger.info(f"Matched: {matched}")
        logger.info(f"Unmatched: {unmatched}")
        logger.info(f"Total processed: {total}")
        logger.info("Docket matcher complete!")

        return 0

    except Exception as e:
        log_error(e, "Docket matcher failed")
        return 1


if __name__ == "__main__":
    exit(main())
