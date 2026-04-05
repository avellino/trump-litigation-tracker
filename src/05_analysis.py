#!/usr/bin/env python3
"""
Step 5: Analysis

Compute aggregate statistics from the enriched database.
Export results as JSON for the visualization app.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from utils import DB_PATH, DATA_DIR, log_error, logger


def compute_overview_stats(conn: sqlite3.Connection) -> dict:
    """Compute overview statistics."""
    total_dockets = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    total_battles = conn.execute("SELECT COUNT(DISTINCT battle_id) FROM cases WHERE battle_id IS NOT NULL").fetchone()[0]
    total_appeals = conn.execute("SELECT COUNT(*) FROM cases WHERE is_appeal = 1").fetchone()[0]
    total_attorneys = conn.execute("SELECT COUNT(DISTINCT name) FROM attorneys").fetchone()[0]
    total_courts = conn.execute("SELECT COUNT(DISTINCT court) FROM cases WHERE court IS NOT NULL").fetchone()[0]

    return {
        "total_dockets": total_dockets,
        "total_battles": total_battles,
        "total_appeals": total_appeals,
        "total_attorneys": total_attorneys,
        "total_courts": total_courts,
        # Keep backward compat
        "total_cases": total_dockets,
    }


def compute_executive_action_counts(conn: sqlite3.Connection) -> list[dict]:
    """Count cases by executive action type, at both docket and battle level."""
    query = """
    SELECT
        COALESCE(base_executive_action, executive_action) as executive_action,
        COUNT(*) as docket_count,
        COUNT(DISTINCT battle_id) as battle_count
    FROM cases
    WHERE COALESCE(base_executive_action, executive_action) IS NOT NULL
      AND COALESCE(base_executive_action, executive_action) != ''
    GROUP BY COALESCE(base_executive_action, executive_action)
    ORDER BY battle_count DESC
    """
    df = pd.read_sql_query(query, conn)
    # Keep backward compat
    df["count"] = df["battle_count"]
    return df.to_dict(orient="records")


def compute_court_counts(conn: sqlite3.Connection) -> list[dict]:
    """Count cases by court."""
    query = """
    SELECT court, court_type, COUNT(*) as count
    FROM cases
    WHERE court IS NOT NULL
    GROUP BY court, court_type
    ORDER BY count DESC
    """
    df = pd.read_sql_query(query, conn)
    return df.to_dict(orient="records")


def compute_top_attorneys(conn: sqlite3.Connection) -> dict:
    """Compute top attorneys by case count (plaintiff and defendant separately)."""
    # Plaintiff attorneys
    plaintiff_query = """
    SELECT name, organization, COUNT(DISTINCT case_id) as case_count
    FROM attorneys
    WHERE role LIKE '%plaintiff%'
    GROUP BY name, organization
    ORDER BY case_count DESC
    LIMIT 50
    """
    plaintiff_df = pd.read_sql_query(plaintiff_query, conn)

    # Defendant attorneys
    defendant_query = """
    SELECT name, organization, COUNT(DISTINCT case_id) as case_count
    FROM attorneys
    WHERE role LIKE '%defendant%'
    GROUP BY name, organization
    ORDER BY case_count DESC
    LIMIT 50
    """
    defendant_df = pd.read_sql_query(defendant_query, conn)

    return {
        "plaintiff": plaintiff_df.to_dict(orient="records"),
        "defendant": defendant_df.to_dict(orient="records"),
    }


def compute_top_organizations(conn: sqlite3.Connection) -> list[dict]:
    """Compute top organizations/firms by case count."""
    query = """
    SELECT organization, role, COUNT(DISTINCT case_id) as case_count
    FROM attorneys
    WHERE organization IS NOT NULL AND organization != ''
    GROUP BY organization, role
    ORDER BY case_count DESC
    LIMIT 50
    """
    df = pd.read_sql_query(query, conn)
    return df.to_dict(orient="records")


def compute_judge_stats(conn: sqlite3.Connection) -> list[dict]:
    """Compute judge assignment statistics."""
    query = """
    SELECT
        judge_name,
        COUNT(*) as case_count,
        SUM(CASE WHEN status LIKE '%injunction%' OR status LIKE '%TRO%' THEN 1 ELSE 0 END) as injunctions,
        SUM(CASE WHEN status LIKE '%dismissed%' OR status LIKE '%denied%' THEN 1 ELSE 0 END) as dismissed
    FROM cases
    WHERE judge_name IS NOT NULL AND judge_name != ''
    GROUP BY judge_name
    ORDER BY case_count DESC
    LIMIT 50
    """
    df = pd.read_sql_query(query, conn)

    # Calculate percentages
    results = []
    for _, row in df.iterrows():
        case_count = row["case_count"]
        injunctions = row["injunctions"] or 0
        dismissed = row["dismissed"] or 0

        results.append(
            {
                "judge_name": row["judge_name"],
                "case_count": case_count,
                "injunctions": injunctions,
                "dismissed": dismissed,
                "injunction_rate": round(injunctions / case_count * 100, 1) if case_count > 0 else 0,
                "dismissal_rate": round(dismissed / case_count * 100, 1) if case_count > 0 else 0,
            }
        )

    return results


def compute_timeline(conn: sqlite3.Connection) -> list[dict]:
    """Compute filing timeline by week."""
    query = """
    SELECT date_filed
    FROM cases
    WHERE date_filed IS NOT NULL
    ORDER BY date_filed
    """
    df = pd.read_sql_query(query, conn)

    if df.empty:
        return []

    df["date_filed"] = pd.to_datetime(df["date_filed"])
    df["week"] = df["date_filed"].dt.to_period("W")
    weekly_counts = df.groupby("week").size().reset_index(name="count")

    results = []
    for _, row in weekly_counts.iterrows():
        results.append(
            {
                "week": str(row["week"]),
                "count": int(row["count"]),
            }
        )

    return results


def compute_outcome_distribution(conn: sqlite3.Connection) -> list[dict]:
    """Compute outcome distribution from status field."""
    query = """
    SELECT status, COUNT(*) as count
    FROM cases
    WHERE status IS NOT NULL AND status != ''
    GROUP BY status
    ORDER BY count DESC
    """
    df = pd.read_sql_query(query, conn)
    return df.to_dict(orient="records")


def compute_case_details(conn: sqlite3.Connection) -> list[dict]:
    """Get all case details for the case explorer."""
    query = """
    SELECT
        id,
        case_name,
        docket_number,
        court,
        court_type,
        judge_name,
        executive_action,
        base_executive_action,
        status,
        date_filed,
        date_terminated,
        summary,
        courtlistener_docket_id,
        battle_id,
        is_appeal,
        parent_docket
    FROM cases
    ORDER BY date_filed DESC, case_name
    """
    df = pd.read_sql_query(query, conn)

    # Add CourtListener link
    df["courtlistener_url"] = df["courtlistener_docket_id"].apply(
        lambda x: f"https://www.courtlistener.com/docket/{x}/" if pd.notna(x) else None
    )

    return df.to_dict(orient="records")


def compute_attorney_network_data(conn: sqlite3.Connection) -> list[dict]:
    """Compute attorney co-occurrence data for network visualization."""
    # Get attorney-case relationships
    query = """
    SELECT name, organization, role,
           GROUP_CONCAT(case_id) as cases
    FROM attorneys
    GROUP BY name, organization, role
    HAVING COUNT(DISTINCT case_id) > 1
    """
    df = pd.read_sql_query(query, conn)

    results = []
    for _, row in df.iterrows():
        case_ids = row["cases"].split(",") if row["cases"] else []
        results.append(
            {
                "name": row["name"],
                "organization": row["organization"],
                "role": row["role"],
                "case_count": len(case_ids),
                "cases": case_ids,
            }
        )

    return sorted(results, key=lambda x: x["case_count"], reverse=True)[:50]


def main():
    """Main entry point."""
    logger.info("=== Step 5: Analysis ===")
    logger.info("Starting...")

    try:
        conn = sqlite3.connect(DB_PATH)

        # Compute all statistics
        logger.info("Computing overview stats...")
        overview = compute_overview_stats(conn)

        logger.info("Computing executive action counts...")
        executive_actions = compute_executive_action_counts(conn)

        logger.info("Computing court counts...")
        court_counts = compute_court_counts(conn)

        logger.info("Computing top attorneys...")
        top_attorneys = compute_top_attorneys(conn)

        logger.info("Computing top organizations...")
        top_orgs = compute_top_organizations(conn)

        logger.info("Computing judge stats...")
        judge_stats = compute_judge_stats(conn)

        logger.info("Computing timeline...")
        timeline = compute_timeline(conn)

        logger.info("Computing outcome distribution...")
        outcomes = compute_outcome_distribution(conn)

        logger.info("Computing case details...")
        case_details = compute_case_details(conn)

        logger.info("Computing attorney network data...")
        network_data = compute_attorney_network_data(conn)

        # Compile all analysis results
        analysis = {
            "generated_at": datetime.now().isoformat(),
            "overview": overview,
            "executive_actions": executive_actions,
            "court_counts": court_counts,
            "top_attorneys": top_attorneys,
            "top_organizations": top_orgs,
            "judge_stats": judge_stats,
            "timeline": timeline,
            "outcomes": outcomes,
            "case_details": case_details,
            "network_data": network_data,
        }

        # Export to JSON
        output_path = DATA_DIR / "enriched" / "analysis.json"
        with open(output_path, "w") as f:
            json.dump(analysis, f, indent=2, default=str)

        logger.info(f"Analysis exported to {output_path}")

        # Print summary
        logger.info("=== Summary ===")
        logger.info(f"Total cases: {overview['total_cases']}")
        logger.info(f"Total attorneys: {overview['total_attorneys']}")
        logger.info(f"Total courts: {overview['total_courts']}")
        logger.info("Analysis complete!")

        conn.close()
        return 0

    except Exception as e:
        log_error(e, "Analysis failed")
        return 1


if __name__ == "__main__":
    exit(main())
