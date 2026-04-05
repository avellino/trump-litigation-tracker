#!/usr/bin/env python3
"""
Step 6: Assign Battle IDs

Cluster related dockets into legal "battles".
Can be run independently on an existing database without re-running enrichment.
"""

import re
import sqlite3
from typing import Optional

from utils import DB_PATH, log_error, logger


def parse_appeal_info(executive_action: str) -> tuple:
    """Parse appeal references from executive_action field."""
    if not executive_action:
        return ("", 0, None)

    match = re.search(
        r"(.+?)\s+Appeal\s+of\s+([\d:]+[-\w,;\s]+\d+)",
        executive_action,
        re.IGNORECASE,
    )
    if match:
        base_action = match.group(1).strip().rstrip(",;")
        parent_ref = match.group(2).strip()
        first_docket = re.match(r"([\d:]+-[\w]+-[\d]+|\d+-\d+)", parent_ref)
        parent_docket = first_docket.group(1) if first_docket else parent_ref
        return (base_action, 1, parent_docket)

    return (executive_action.strip(), 0, None)


def normalize_plaintiff(case_name: str) -> str:
    """Extract and normalize plaintiff for grouping."""
    parts = re.split(r"\s+v\.?\s+", case_name, maxsplit=1, flags=re.IGNORECASE)
    plaintiff = parts[0].strip().lower()

    if ";" in plaintiff:
        plaintiff = plaintiff.split(";")[0].strip()

    plaintiff = re.sub(r"^state of\s+", "", plaintiff)
    plaintiff = re.sub(r"^commonwealth of\s+", "", plaintiff)
    plaintiff = re.sub(r",?\s*(inc|llc|et al|pllc|l\.?l\.?c)\.?.*$", "", plaintiff)
    plaintiff = re.sub(r"\s+", " ", plaintiff).strip()

    return plaintiff


def main():
    logger.info("=== Step 6: Assign Battle IDs ===")

    conn = sqlite3.connect(DB_PATH)

    # Add columns if they don't exist
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
    for col, coldef in [
        ("battle_id", "INTEGER"),
        ("is_appeal", "INTEGER DEFAULT 0"),
        ("parent_docket", "TEXT"),
        ("base_executive_action", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {coldef}")
            logger.info(f"Added column: {col}")

    # Create index if not exists
    conn.executescript("CREATE INDEX IF NOT EXISTS idx_cases_battle ON cases(battle_id);")

    # Step 1: Parse appeal info for every row
    rows = conn.execute("SELECT id, executive_action FROM cases").fetchall()
    for case_id, ea in rows:
        base_action, is_appeal, parent_docket = parse_appeal_info(ea or "")
        conn.execute(
            "UPDATE cases SET base_executive_action = ?, is_appeal = ?, parent_docket = ? WHERE id = ?",
            (base_action, is_appeal, parent_docket, case_id),
        )
    conn.commit()

    appeals = conn.execute("SELECT COUNT(*) FROM cases WHERE is_appeal = 1").fetchone()[0]
    logger.info(f"Identified {appeals} appeal dockets")

    # Step 2: Build docket_number -> case_id lookup
    all_rows = conn.execute(
        "SELECT id, case_name, docket_number, base_executive_action, is_appeal, parent_docket FROM cases"
    ).fetchall()

    docket_to_ids = {}
    for row in all_rows:
        if row[2]:
            docket_to_ids.setdefault(row[2], []).append(row[0])

    # Step 3: Link appeals to parents
    parent_map = {}
    for row in all_rows:
        case_id, _, _, _, is_appeal, parent_docket = row
        if is_appeal and parent_docket:
            parent_ids = docket_to_ids.get(parent_docket, [])
            if parent_ids:
                parent_map[case_id] = parent_ids[0]

    # Step 4: Group by (base_action, normalized_plaintiff)
    from collections import defaultdict

    group_key_to_ids = defaultdict(list)
    case_to_group_key = {}

    for row in all_rows:
        case_id, case_name, docket_num, base_action, is_appeal, parent_docket = row

        if case_id in parent_map:
            continue  # appeals handled below

        plaintiff = normalize_plaintiff(case_name)
        action = (base_action or "").strip().lower()
        gk = (action, plaintiff)

        group_key_to_ids[gk].append(case_id)
        case_to_group_key[case_id] = gk

    # Step 5: Assign appeals to parent's group
    for appeal_id, parent_id in parent_map.items():
        if parent_id in case_to_group_key:
            gk = case_to_group_key[parent_id]
            group_key_to_ids[gk].append(appeal_id)
            case_to_group_key[appeal_id] = gk
        else:
            row_data = next(r for r in all_rows if r[0] == appeal_id)
            plaintiff = normalize_plaintiff(row_data[1])
            action = (row_data[3] or "").strip().lower()
            gk = (action, plaintiff)
            group_key_to_ids[gk].append(appeal_id)
            case_to_group_key[appeal_id] = gk

    # Step 6: Assign sequential battle_ids
    battle_id = 0
    assigned = 0
    for gk, case_ids in group_key_to_ids.items():
        battle_id += 1
        for cid in case_ids:
            conn.execute("UPDATE cases SET battle_id = ? WHERE id = ?", (battle_id, cid))
            assigned += 1

    conn.commit()

    # Summary
    logger.info(f"Assigned {assigned} dockets to {battle_id} legal battles")

    # Sanity checks
    no_battle = conn.execute("SELECT COUNT(*) FROM cases WHERE battle_id IS NULL").fetchone()[0]
    if no_battle:
        logger.warning(f"{no_battle} cases have no battle_id!")

    # Show battle size distribution
    dist = conn.execute("""
        SELECT dockets, COUNT(*) as battles FROM (
            SELECT battle_id, COUNT(*) as dockets FROM cases GROUP BY battle_id
        ) GROUP BY dockets ORDER BY dockets
    """).fetchall()
    logger.info("Battle size distribution:")
    for dockets, battles in dist:
        logger.info(f"  {dockets} docket(s): {battles} battles")

    conn.close()
    logger.info("Battle assignment complete!")
    return 0


if __name__ == "__main__":
    exit(main())
