#!/usr/bin/env python3
"""
Trump Administration Litigation Tracker — Streamlit Visualization App

Reads from the enriched SQLite database (data/enriched/cases.db) and
the pre-computed analysis JSON (data/enriched/analysis.json).
"""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "enriched" / "cases.db"
ANALYSIS_PATH = BASE_DIR / "data" / "enriched" / "analysis.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@st.cache_resource
def get_db_connection() -> sqlite3.Connection:
    if not DB_PATH.exists():
        st.error(f"Database not found at {DB_PATH}. Run the pipeline first.")
        st.stop()
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@st.cache_data(ttl=60)
def load_analysis() -> dict:
    if ANALYSIS_PATH.exists():
        with open(ANALYSIS_PATH) as f:
            return json.load(f)
    return {}


def query_df(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn)


# ---------------------------------------------------------------------------
# Page: Overview Dashboard
# ---------------------------------------------------------------------------

def page_overview(conn: sqlite3.Connection, analysis: dict):
    st.header("Overview Dashboard")

    overview = analysis.get("overview", {})

    # Top-level metrics: battles + dockets side by side
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Legal Battles", overview.get("total_battles", "---"))
    c2.metric("Total Dockets", overview.get("total_dockets", "---"))
    c3.metric("Appeals", overview.get("total_appeals", "---"))
    c4.metric("Attorneys", overview.get("total_attorneys", "---"))
    c5.metric("Courts", overview.get("total_courts", "---"))

    st.caption(
        "A **legal battle** groups a district court filing with its appeals and "
        "related proceedings. **Dockets** counts every individual court filing."
    )

    st.divider()

    # Toggle: count by battles or dockets
    count_mode = st.radio(
        "Count by", ["Legal Battles", "Individual Dockets"],
        horizontal=True, key="overview_count_mode",
    )
    count_col = "battle_count" if count_mode == "Legal Battles" else "docket_count"
    count_label = "Battles" if count_mode == "Legal Battles" else "Dockets"

    # Cases by executive action
    ea_data = analysis.get("executive_actions", [])
    if ea_data:
        df = pd.DataFrame(ea_data)
        if count_col in df.columns:
            df["display_count"] = df[count_col]
        else:
            df["display_count"] = df.get("count", 0)

        fig = px.bar(
            df.head(20), x="display_count", y="executive_action",
            orientation="h",
            title=f"{count_label} by Executive Action (top 20)",
            labels={"display_count": count_label, "executive_action": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=500)
        st.plotly_chart(fig, use_container_width=True)

    # Cases by court
    court_data = analysis.get("court_counts", [])
    if court_data:
        df = pd.DataFrame(court_data)
        fig = px.bar(
            df.head(20), x="count", y="court",
            orientation="h", title="Dockets by Court (top 20)",
            labels={"count": "Dockets", "court": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=500)
        st.plotly_chart(fig, use_container_width=True)

    # Timeline
    timeline = analysis.get("timeline", [])
    if timeline:
        df = pd.DataFrame(timeline)
        fig = px.line(
            df, x="week", y="count",
            title="Dockets Filed Over Time (by week)",
            labels={"week": "Week", "count": "Dockets Filed"},
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Attorney Network
# ---------------------------------------------------------------------------

def page_attorneys(conn: sqlite3.Connection, analysis: dict):
    st.header("Attorney Network")

    top = analysis.get("top_attorneys", {})

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Top Plaintiff-Side Attorneys")
        p_data = top.get("plaintiff", [])
        if p_data:
            st.dataframe(pd.DataFrame(p_data).head(20), use_container_width=True, hide_index=True)
        else:
            st.info("No plaintiff attorney data yet.")

    with col2:
        st.subheader("Top Defendant-Side Attorneys (DOJ)")
        d_data = top.get("defendant", [])
        if d_data:
            st.dataframe(pd.DataFrame(d_data).head(20), use_container_width=True, hide_index=True)
        else:
            st.info("No defendant attorney data yet.")

    # Organizations
    st.subheader("Top Organizations / Firms")
    orgs = analysis.get("top_organizations", [])
    if orgs:
        df = pd.DataFrame(orgs)
        fig = px.bar(
            df.head(25), x="case_count", y="organization",
            color="role", orientation="h",
            title="Organizations by Case Count",
            labels={"case_count": "Cases", "organization": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=600)
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Judge Analysis
# ---------------------------------------------------------------------------

def page_judges(conn: sqlite3.Connection, analysis: dict):
    st.header("Judge Analysis")

    judge_data = analysis.get("judge_stats", [])
    if not judge_data:
        st.info("No judge data available. Run pipeline steps 2-4 first.")
        return

    df = pd.DataFrame(judge_data)

    st.subheader("Judges by Case Count")
    st.dataframe(df.head(30), use_container_width=True, hide_index=True)

    # Bar chart
    fig = px.bar(
        df.head(20), x="case_count", y="judge_name",
        orientation="h", title="Top 20 Judges by Docket Count",
        labels={"case_count": "Dockets", "judge_name": ""},
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=500)
    st.plotly_chart(fig, use_container_width=True)

    # Outcome rates
    if any(row.get("injunction_rate", 0) > 0 or row.get("dismissal_rate", 0) > 0 for row in judge_data):
        st.subheader("Outcome Rates by Judge")
        fig = px.bar(
            df[df["case_count"] >= 2].head(20),
            x="judge_name", y=["injunction_rate", "dismissal_rate"],
            barmode="group",
            title="Injunction vs Dismissal Rate (judges with 2+ dockets)",
            labels={"value": "Rate (%)", "judge_name": ""},
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Case Explorer
# ---------------------------------------------------------------------------

def page_cases(conn: sqlite3.Connection, analysis: dict):
    st.header("Case Explorer")

    col1, col2 = st.columns([3, 1])
    with col1:
        search = st.text_input("Search cases", placeholder="e.g. Trump, ACLU, birthright...")
    with col2:
        view_mode = st.radio("View", ["All Dockets", "Battles Only"], key="explorer_view")

    if view_mode == "Battles Only":
        # Show one row per battle (the earliest-filed docket in each battle)
        sql = """
        SELECT
            c.case_name as "Case Name",
            c.court as "Court",
            c.judge_name as "Judge",
            COALESCE(c.base_executive_action, c.executive_action) as "Executive Action",
            c.status as "Status",
            c.date_filed as "Date Filed",
            c.docket_number as "Docket #",
            c.courtlistener_docket_id as cl_id,
            c.battle_id,
            b.docket_count as "Related Dockets"
        FROM cases c
        JOIN (
            SELECT battle_id, MIN(id) as first_id, COUNT(*) as docket_count
            FROM cases GROUP BY battle_id
        ) b ON c.battle_id = b.battle_id AND c.id = b.first_id
        ORDER BY c.date_filed DESC, c.case_name
        """
    else:
        sql = """
        SELECT
            case_name as "Case Name",
            court as "Court",
            judge_name as "Judge",
            COALESCE(base_executive_action, executive_action) as "Executive Action",
            status as "Status",
            date_filed as "Date Filed",
            docket_number as "Docket #",
            courtlistener_docket_id as cl_id,
            battle_id,
            CASE WHEN is_appeal = 1 THEN 'Appeal' ELSE '' END as "Type"
        FROM cases
        ORDER BY date_filed DESC, case_name
        """

    df = query_df(conn, sql)

    if search:
        mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
        df = df[mask]

    label = "battles" if view_mode == "Battles Only" else "dockets"
    st.write(f"Showing {len(df)} {label}")

    # Add clickable CL link
    df["CourtListener"] = df["cl_id"].apply(
        lambda x: f"https://www.courtlistener.com/docket/{int(x)}/" if pd.notna(x) else ""
    )
    display_cols = [c for c in df.columns if c not in ("cl_id", "battle_id")]
    display_df = df[display_cols]
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Case detail expander
    if not df.empty:
        selected = st.selectbox("Select a case for details", df["Case Name"].tolist())
        if selected:
            case_row = df[df["Case Name"] == selected].iloc[0]
            battle_id = case_row.get("battle_id")

            with st.expander(f"Details: {selected}", expanded=True):
                for col in display_cols:
                    val = case_row.get(col, "")
                    if val and str(val) != "nan" and str(val) != "":
                        st.write(f"**{col}:** {val}")

                # Show related dockets in the same battle
                if battle_id and pd.notna(battle_id):
                    related = query_df(conn, f"""
                        SELECT case_name, docket_number, court, status,
                               CASE WHEN is_appeal = 1 THEN 'Appeal' ELSE 'Original' END as type
                        FROM cases WHERE battle_id = {int(battle_id)}
                        ORDER BY date_filed
                    """)
                    if len(related) > 1:
                        st.write(f"**Related dockets in this legal battle ({len(related)}):**")
                        st.dataframe(related, use_container_width=True, hide_index=True)

                # Show parties and attorneys
                case_id_result = conn.execute(
                    "SELECT id FROM cases WHERE case_name = ? LIMIT 1", (selected,)
                ).fetchone()
                if case_id_result:
                    cid = case_id_result[0]
                    parties = query_df(conn, f"SELECT name, party_type, organization FROM parties WHERE case_id = {cid}")
                    if not parties.empty:
                        st.write("**Parties:**")
                        st.dataframe(parties, use_container_width=True, hide_index=True)

                    attorneys = query_df(conn, f"SELECT name, role, organization FROM attorneys WHERE case_id = {cid}")
                    if not attorneys.empty:
                        st.write("**Attorneys:**")
                        st.dataframe(attorneys, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page: Executive Action Breakdown
# ---------------------------------------------------------------------------

def page_executive_actions(conn: sqlite3.Connection, analysis: dict):
    st.header("Executive Action Breakdown")

    ea_data = analysis.get("executive_actions", [])
    if not ea_data:
        st.info("No executive action data available.")
        return

    df_ea = pd.DataFrame(ea_data)

    # Show table with both counts
    display_cols = ["executive_action"]
    if "battle_count" in df_ea.columns:
        display_cols += ["battle_count", "docket_count"]
        df_ea = df_ea.rename(columns={
            "executive_action": "Executive Action",
            "battle_count": "Battles",
            "docket_count": "Dockets",
        })
        display_cols = ["Executive Action", "Battles", "Dockets"]
    else:
        df_ea = df_ea.rename(columns={"executive_action": "Executive Action", "count": "Count"})
        display_cols = ["Executive Action", "Count"]

    st.dataframe(df_ea[display_cols], use_container_width=True, hide_index=True)

    # Outcome by executive action (using base_executive_action)
    sql = """
    SELECT
        COALESCE(base_executive_action, executive_action) as executive_action,
        status,
        COUNT(*) as count
    FROM cases
    WHERE COALESCE(base_executive_action, executive_action) IS NOT NULL
      AND COALESCE(base_executive_action, executive_action) != ''
    GROUP BY COALESCE(base_executive_action, executive_action), status
    ORDER BY executive_action, count DESC
    """
    df = query_df(conn, sql)

    if not df.empty:
        top_actions = df_ea.head(10)["Executive Action"].tolist()
        filtered = df[df["executive_action"].isin(top_actions)]

        if not filtered.empty:
            fig = px.bar(
                filtered, x="executive_action", y="count", color="status",
                title="Status Breakdown by Executive Action (top 10)",
                labels={"count": "Dockets", "executive_action": ""},
            )
            fig.update_layout(xaxis_tickangle=-45, height=500)
            st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="Trump Admin Litigation Tracker",
        page_icon="---",
        layout="wide",
    )

    st.title("Trump Administration Litigation Tracker")
    st.caption("Enriched with data from CourtListener RECAP Archive")

    conn = get_db_connection()
    analysis = load_analysis()

    page = st.sidebar.radio("Navigate", [
        "Overview Dashboard",
        "Attorney Network",
        "Judge Analysis",
        "Case Explorer",
        "Executive Action Breakdown",
    ])

    if page == "Overview Dashboard":
        page_overview(conn, analysis)
    elif page == "Attorney Network":
        page_attorneys(conn, analysis)
    elif page == "Judge Analysis":
        page_judges(conn, analysis)
    elif page == "Case Explorer":
        page_cases(conn, analysis)
    elif page == "Executive Action Breakdown":
        page_executive_actions(conn, analysis)


if __name__ == "__main__":
    main()
