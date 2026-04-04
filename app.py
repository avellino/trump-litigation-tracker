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
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Cases", overview.get("total_cases", "—"))
    c2.metric("Total Attorneys", overview.get("total_attorneys", "—"))
    c3.metric("Total Courts", overview.get("total_courts", "—"))

    # Cases by executive action
    ea_data = analysis.get("executive_actions", [])
    if ea_data:
        df = pd.DataFrame(ea_data)
        fig = px.bar(
            df.head(20), x="count", y="executive_action",
            orientation="h", title="Cases by Executive Action (top 20)",
            labels={"count": "Cases", "executive_action": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=500)
        st.plotly_chart(fig, use_container_width=True)

    # Cases by court
    court_data = analysis.get("court_counts", [])
    if court_data:
        df = pd.DataFrame(court_data)
        fig = px.bar(
            df.head(20), x="count", y="court",
            orientation="h", title="Cases by Court (top 20)",
            labels={"count": "Cases", "court": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=500)
        st.plotly_chart(fig, use_container_width=True)

    # Timeline
    timeline = analysis.get("timeline", [])
    if timeline:
        df = pd.DataFrame(timeline)
        fig = px.line(
            df, x="week", y="count",
            title="Cases Filed Over Time (by week)",
            labels={"week": "Week", "count": "Cases Filed"},
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
        orientation="h", title="Top 20 Judges by Case Count",
        labels={"case_count": "Cases", "judge_name": ""},
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
            title="Injunction vs Dismissal Rate (judges with 2+ cases)",
            labels={"value": "Rate (%)", "judge_name": ""},
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Page: Case Explorer
# ---------------------------------------------------------------------------

def page_cases(conn: sqlite3.Connection, analysis: dict):
    st.header("Case Explorer")

    search = st.text_input("Search cases", placeholder="e.g. Trump, ACLU, birthright...")

    sql = """
    SELECT
        case_name as "Case Name",
        court as "Court",
        judge_name as "Judge",
        executive_action as "Executive Action",
        status as "Status",
        date_filed as "Date Filed",
        docket_number as "Docket #",
        courtlistener_docket_id as cl_id
    FROM cases
    ORDER BY date_filed DESC, case_name
    """
    df = query_df(conn, sql)

    if search:
        mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
        df = df[mask]

    st.write(f"Showing {len(df)} cases")

    # Add clickable CL link
    df["CourtListener"] = df["cl_id"].apply(
        lambda x: f"https://www.courtlistener.com/docket/{int(x)}/" if pd.notna(x) else ""
    )
    display_df = df.drop(columns=["cl_id"])
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # Case detail expander
    if not df.empty:
        selected = st.selectbox("Select a case for details", df["Case Name"].tolist())
        if selected:
            case_row = df[df["Case Name"] == selected].iloc[0]
            with st.expander(f"Details: {selected}", expanded=True):
                for col in display_df.columns:
                    val = case_row.get(col, "")
                    if val and str(val) != "nan":
                        st.write(f"**{col}:** {val}")

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
    st.dataframe(df_ea, use_container_width=True, hide_index=True)

    # Outcome by executive action
    sql = """
    SELECT
        executive_action,
        status,
        COUNT(*) as count
    FROM cases
    WHERE executive_action IS NOT NULL AND executive_action != ''
    GROUP BY executive_action, status
    ORDER BY executive_action, count DESC
    """
    df = query_df(conn, sql)

    if not df.empty:
        # Top actions
        top_actions = df_ea.head(10)["executive_action"].tolist()
        filtered = df[df["executive_action"].isin(top_actions)]

        if not filtered.empty:
            fig = px.bar(
                filtered, x="executive_action", y="count", color="status",
                title="Status Breakdown by Executive Action (top 10)",
                labels={"count": "Cases", "executive_action": ""},
            )
            fig.update_layout(xaxis_tickangle=-45, height=500)
            st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="Trump Admin Litigation Tracker",
        page_icon="⚖️",
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
