#!/usr/bin/env python3
"""
Trump Administration Litigation Tracker — Streamlit Visualization App

Reads from the enriched SQLite database (data/enriched/cases.db) and
the pre-computed analysis JSON (data/enriched/analysis.json).
"""

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "enriched" / "cases.db"
ANALYSIS_PATH = BASE_DIR / "data" / "enriched" / "analysis.json"


# ---------------------------------------------------------------------------
# Court name lookup
# ---------------------------------------------------------------------------

COURT_NAMES = {
    "dcd": "D.C. District Court",
    "cadc": "D.C. Circuit Court of Appeals",
    "mad": "D. Mass.",
    "mdd": "D. Md.",
    "ca9": "Ninth Circuit",
    "nysd": "S.D.N.Y.",
    "cand": "N.D. Cal.",
    "ca1": "First Circuit",
    "rid": "D.R.I.",
    "ca4": "Fourth Circuit",
    "ilnd": "N.D. Ill.",
    "wawd": "W.D. Wash.",
    "mnd": "D. Minn.",
    "cit": "Court of International Trade",
    "ca2": "Second Circuit",
    "cacd": "C.D. Cal.",
    "ord": "D. Or.",
    "njd": "D.N.J.",
    "cod": "D. Colo.",
    "gamd": "M.D. Ga.",
    "nhd": "D.N.H.",
    "vaed": "E.D. Va.",
    "nynd": "N.D.N.Y.",
    "paed": "E.D. Pa.",
    "nyed": "E.D.N.Y.",
    "txsd": "S.D. Tex.",
    "scd": "D.S.C.",
    "vtd": "D. Vt.",
    "txwd": "W.D. Tex.",
    "kyed": "E.D. Ky.",
    "nvd": "D. Nev.",
    "mtd": "D. Mont.",
    "med": "D. Me.",
    "wvsd": "S.D. W.Va.",
    "ca3": "Third Circuit",
    "azd": "D. Ariz.",
    "wiwd": "W.D. Wis.",
    "casd": "S.D. Cal.",
    "pawd": "W.D. Pa.",
    "txnd": "N.D. Tex.",
    "ca5": "Fifth Circuit",
    "ca10": "Tenth Circuit",
    "lawd": "W.D. La.",
    "hid": "D. Haw.",
    "miwd": "W.D. Mich.",
    "tnmd": "M.D. Tenn.",
    "cafc": "Federal Circuit",
    "pamd": "M.D. Pa.",
    "uscfc": "U.S. Court of Federal Claims",
    "akd": "D. Alaska",
    "ca7": "Seventh Circuit",
    "ca6": "Sixth Circuit",
    "flmd": "M.D. Fla.",
    "ilsd": "S.D. Ill.",
    "alsd": "S.D. Ala.",
    "ca8": "Eighth Circuit",
    "flsd": "S.D. Fla.",
    "gand": "N.D. Ga.",
    "ncwd": "W.D.N.C.",
}


def court_display_name(court_value: str) -> str:
    """Convert a CourtListener API URL to a readable court name."""
    if not court_value or not isinstance(court_value, str) or not court_value.startswith("http"):
        return court_value
    match = re.search(r'/courts/([^/]+)/?$', court_value)
    if match:
        slug = match.group(1)
        return COURT_NAMES.get(slug, slug.upper())
    return court_value


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


@st.cache_data(ttl=300)
def _db_columns(_conn: sqlite3.Connection, table: str = "cases") -> set:
    """Return set of column names for a table."""
    cols = _conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in cols}


def has_column(conn: sqlite3.Connection, col: str) -> bool:
    return col in _db_columns(conn)


def ea_expr(conn: sqlite3.Connection, prefix: str = "") -> str:
    """Return the SQL expression for executive action, adapting to available columns.

    Args:
        prefix: Optional table alias prefix, e.g. "c." for use in JOINs.
    """
    p = f"{prefix}." if prefix and not prefix.endswith(".") else prefix
    if has_column(conn, "base_executive_action"):
        return f"COALESCE({p}base_executive_action, {p}executive_action)"
    return f"{p}executive_action"


def build_org_network(conn: sqlite3.Connection, min_shared_cases: int = 3):
    """Build a co-litigation network of plaintiff-side organizations.

    Returns a plotly Figure showing organizations as nodes and shared cases as edges.
    Returns None if insufficient data.
    """
    sql = f"""
        SELECT a1.organization AS org1, a2.organization AS org2,
               COUNT(DISTINCT a1.case_id) AS shared_cases
        FROM attorneys a1
        JOIN attorneys a2
          ON a1.case_id = a2.case_id
          AND a1.organization < a2.organization
        WHERE a1.role = 'plaintiff_attorney'
          AND a2.role = 'plaintiff_attorney'
          AND a1.organization IS NOT NULL
          AND a2.organization IS NOT NULL
          AND a1.organization != ''
          AND a2.organization != ''
        GROUP BY a1.organization, a2.organization
        HAVING shared_cases >= {min_shared_cases}
        ORDER BY shared_cases DESC
    """
    edges_df = query_df(conn, sql)

    if edges_df.empty:
        return None

    # Total case counts per org for node sizing
    org_counts_sql = """
        SELECT organization, COUNT(DISTINCT case_id) AS case_count
        FROM attorneys
        WHERE role = 'plaintiff_attorney'
          AND organization IS NOT NULL AND organization != ''
        GROUP BY organization
    """
    org_counts = query_df(conn, org_counts_sql)
    org_size = dict(zip(org_counts["organization"], org_counts["case_count"]))

    # Build node list from edges
    all_orgs = sorted(set(edges_df["org1"].tolist() + edges_df["org2"].tolist()))
    n = len(all_orgs)

    # Circular layout as default
    import math
    positions = {}
    for i, org in enumerate(all_orgs):
        angle = 2 * math.pi * i / n
        positions[org] = (math.cos(angle), math.sin(angle))

    # Use spring layout if networkx is available
    try:
        import networkx as nx
        G = nx.Graph()
        for _, row in edges_df.iterrows():
            G.add_edge(row["org1"], row["org2"], weight=row["shared_cases"])
        positions = nx.spring_layout(G, k=2.0 / math.sqrt(n), iterations=50, seed=42)
    except ImportError:
        pass

    max_weight = edges_df["shared_cases"].max()

    fig = go.Figure()

    # Edge traces with weight-based styling
    for _, row in edges_df.iterrows():
        x0, y0 = positions[row["org1"]]
        x1, y1 = positions[row["org2"]]
        weight = row["shared_cases"]
        opacity = 0.2 + 0.6 * (weight / max_weight)
        width = 0.5 + 2.5 * (weight / max_weight)

        fig.add_trace(go.Scatter(
            x=[x0, x1], y=[y0, y1],
            mode="lines",
            line=dict(width=width, color=f"rgba(100,100,100,{opacity})"),
            hoverinfo="text",
            hovertext=f"{row['org1']} ↔ {row['org2']}: {weight} shared cases",
            showlegend=False,
        ))

    # Node trace
    node_x = [positions[org][0] for org in all_orgs]
    node_y = [positions[org][1] for org in all_orgs]
    node_sizes = [max(8, min(40, org_size.get(org, 1) * 1.5)) for org in all_orgs]
    node_text = [f"{org}<br>{org_size.get(org, 0)} cases" for org in all_orgs]
    node_labels = [org if len(org) <= 25 else org[:22] + "..." for org in all_orgs]

    fig.add_trace(go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        text=node_labels,
        textposition="top center",
        textfont=dict(size=9),
        hovertext=node_text,
        hoverinfo="text",
        marker=dict(
            size=node_sizes,
            color="#2166ac",
            line=dict(width=1, color="white"),
        ),
        showlegend=False,
    ))

    fig.update_layout(
        title=dict(text="Plaintiff Organization Co-Litigation Network", font=dict(size=16)),
        showlegend=False,
        hovermode="closest",
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        height=600,
        margin=dict(l=20, r=20, t=50, b=20),
    )

    return fig


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
        df["court"] = df["court"].apply(court_display_name)
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
        granularity = st.radio(
            "Timeline granularity",
            ["Monthly", "Weekly", "Cumulative"],
            horizontal=True,
            key="timeline_granularity",
        )

        df = pd.DataFrame(timeline)

        if granularity == "Monthly":
            # Parse start date from week range and aggregate by month
            df["month"] = df["week"].str.split("/").str[0].str[:7]
            df_monthly = df.groupby("month", as_index=False)["count"].sum()
            df_monthly["month_label"] = pd.to_datetime(df_monthly["month"]).dt.strftime("%b %Y")
            fig = px.bar(
                df_monthly, x="month_label", y="count",
                title="Dockets Filed Over Time (monthly)",
                labels={"month_label": "", "count": "Dockets Filed"},
            )
            fig.update_layout(xaxis_tickangle=-45, height=400)

        elif granularity == "Weekly":
            fig = px.line(
                df, x="week", y="count",
                title="Dockets Filed Over Time (weekly)",
                labels={"week": "", "count": "Dockets Filed"},
            )
            fig.update_layout(xaxis_tickangle=-45, xaxis_nticks=20, height=400)

        else:  # Cumulative
            df["cumulative"] = df["count"].cumsum()
            fig = px.line(
                df, x="week", y="cumulative",
                title="Cumulative Dockets Filed Over Time",
                labels={"week": "", "cumulative": "Total Dockets Filed"},
            )
            fig.update_layout(xaxis_tickangle=-45, xaxis_nticks=20, height=400)

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

    # Co-litigation network
    st.divider()
    st.subheader("Co-Litigation Network")
    st.caption(
        "Organizations that appear together as plaintiff-side counsel on the same dockets. "
        "Node size = total cases for that organization. Edges connect organizations that "
        "co-appear on 3+ dockets together."
    )

    min_shared = st.slider(
        "Minimum shared cases to show a connection",
        min_value=2, max_value=10, value=3, step=1,
        key="network_min_shared",
    )

    network_fig = build_org_network(conn, min_shared_cases=min_shared)
    if network_fig:
        st.plotly_chart(network_fig, use_container_width=True)
    else:
        st.info("Not enough co-litigation data to build a network at this threshold.")


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

    # Appointer distribution (show first if data available)
    appointer_stats = analysis.get("appointer_stats", {})
    appointer_dist = appointer_stats.get("distribution", [])
    if appointer_dist:
        st.subheader("Cases by Appointing President")

        col1, col2 = st.columns([2, 1])
        with col1:
            df_app = pd.DataFrame(appointer_dist)
            # Filter out "Unknown" for the chart
            df_known = df_app[df_app["appointed_by"] != "Unknown"]
            if not df_known.empty:
                # Define party colors for presidents
                president_colors = {
                    "Barack Obama": "#2166ac",
                    "Donald Trump": "#b2182b",
                    "Joe Biden": "#2166ac",
                    "George W. Bush": "#b2182b",
                    "Bill Clinton": "#2166ac",
                    "Ronald Reagan": "#b2182b",
                    "George H.W. Bush": "#b2182b",
                    "Jimmy Carter": "#2166ac",
                    "Richard Nixon": "#b2182b",
                    "Lyndon B. Johnson": "#2166ac",
                    "Gerald Ford": "#b2182b",
                    "John F. Kennedy": "#2166ac",
                    "Dwight D. Eisenhower": "#b2182b",
                }
                df_known["color"] = df_known["appointed_by"].map(
                    lambda x: president_colors.get(x, "#999999")
                )
                fig = px.bar(
                    df_known, x="case_count", y="appointed_by",
                    orientation="h",
                    title="Dockets by Appointing President",
                    labels={"case_count": "Dockets", "appointed_by": ""},
                    color="appointed_by",
                    color_discrete_map=president_colors,
                )
                fig.update_layout(
                    yaxis={"categoryorder": "total ascending"},
                    showlegend=False,
                    height=400,
                )
                st.plotly_chart(fig, use_container_width=True)

        with col2:
            total_with = appointer_stats.get("total_with_data", 0)
            total_judges = appointer_stats.get("total_judges_with_data", 0)
            st.metric("Judges with Appointer Data", total_judges)
            st.metric("Cases Covered", total_with)

            # Show party breakdown
            dem_presidents = {"Barack Obama", "Joe Biden", "Bill Clinton", "Jimmy Carter",
                            "Lyndon B. Johnson", "John F. Kennedy", "Harry S. Truman"}
            rep_presidents = {"Donald Trump", "George W. Bush", "George H.W. Bush",
                            "Ronald Reagan", "Richard Nixon", "Gerald Ford", "Dwight D. Eisenhower"}
            df_known_data = pd.DataFrame(appointer_dist)
            df_known_data = df_known_data[df_known_data["appointed_by"] != "Unknown"]
            dem_cases = df_known_data[df_known_data["appointed_by"].isin(dem_presidents)]["case_count"].sum()
            rep_cases = df_known_data[df_known_data["appointed_by"].isin(rep_presidents)]["case_count"].sum()
            if dem_cases or rep_cases:
                st.write("**By party of appointing president:**")
                st.write(f"🔵 Democrat appointees: {int(dem_cases)} cases")
                st.write(f"🔴 Republican appointees: {int(rep_cases)} cases")

        st.divider()

    st.subheader("Judges by Case Count")

    # Color-code judges by appointer if data available
    if "appointed_by" in df.columns:
        display_df = df.head(30).rename(columns={
            "judge_name": "Judge",
            "appointed_by": "Appointed By",
            "case_count": "Cases",
            "injunctions": "Injunctions",
            "dismissed": "Dismissed",
            "injunction_rate": "Injunction %",
            "dismissal_rate": "Dismissal %",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)
    else:
        st.dataframe(df.head(30), use_container_width=True, hide_index=True)

    # Bar chart colored by appointer
    chart_df = df.head(20).copy()
    if "appointed_by" in chart_df.columns:
        president_colors = {
            "Barack Obama": "#2166ac",
            "Donald Trump": "#b2182b",
            "Joe Biden": "#2166ac",
            "George W. Bush": "#b2182b",
            "Bill Clinton": "#2166ac",
            "Ronald Reagan": "#b2182b",
            "George H.W. Bush": "#b2182b",
            "Jimmy Carter": "#2166ac",
            "Unknown": "#999999",
        }
        fig = px.bar(
            chart_df, x="case_count", y="judge_name",
            color="appointed_by",
            orientation="h", title="Top 20 Judges by Docket Count",
            labels={"case_count": "Dockets", "judge_name": "", "appointed_by": "Appointed By"},
            color_discrete_map=president_colors,
        )
    else:
        fig = px.bar(
            chart_df, x="case_count", y="judge_name",
            orientation="h", title="Top 20 Judges by Docket Count",
            labels={"case_count": "Dockets", "judge_name": ""},
        )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=500)
    st.plotly_chart(fig, use_container_width=True)

    # Outcome rates — scatter plot
    if any(row.get("injunction_rate", 0) > 0 or row.get("dismissal_rate", 0) > 0 for row in judge_data):
        st.subheader("Outcome Rates by Judge")

        scatter_df = df[df["case_count"] >= 2].copy()

        if "appointed_by" in scatter_df.columns:
            color_col = "appointed_by"
            color_map = president_colors
        else:
            color_col = None
            color_map = None

        fig = px.scatter(
            scatter_df,
            x="dismissal_rate",
            y="injunction_rate",
            size="case_count",
            color=color_col,
            color_discrete_map=color_map,
            hover_name="judge_name",
            hover_data={"case_count": True, "injunction_rate": ":.1f", "dismissal_rate": ":.1f"},
            title="Injunction Rate vs Dismissal Rate (judges with 2+ dockets)",
            labels={
                "dismissal_rate": "Dismissal Rate (%)",
                "injunction_rate": "Injunction Rate (%)",
                "case_count": "Docket Count",
                "appointed_by": "Appointed By",
            },
            size_max=30,
        )

        fig.update_layout(
            height=500,
            xaxis=dict(range=[-2, max(scatter_df["dismissal_rate"].max() + 5, 40)]),
            yaxis=dict(range=[-2, max(scatter_df["injunction_rate"].max() + 5, 40)]),
        )

        st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "Each dot is a judge. Dot size = number of dockets assigned. "
            "Color = party of appointing president. Hover for details."
        )


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

    ea = ea_expr(conn)
    has_battle = has_column(conn, "battle_id")
    has_appeal = has_column(conn, "is_appeal")
    has_appt = has_column(conn, "appointed_by")

    appt_col_c = ", c.appointed_by as \"Appointed By\"" if has_appt else ""
    appt_col = ", appointed_by as \"Appointed By\"" if has_appt else ""

    ea_c = ea_expr(conn, "c")

    if view_mode == "Battles Only" and has_battle:
        sql = f"""
        SELECT
            c.case_name as "Case Name",
            c.court as "Court",
            c.judge_name as "Judge"
            {appt_col_c},
            {ea_c} as "Executive Action",
            c.status as "Status",
            c.date_filed as "Date Filed",
            c.docket_number as "Docket #",
            c.courtlistener_url as cl_url,
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
        appeal_col = ", CASE WHEN is_appeal = 1 THEN 'Appeal' ELSE '' END as \"Type\"" if has_appeal else ""
        battle_col = ", battle_id" if has_battle else ""
        sql = f"""
        SELECT
            case_name as "Case Name",
            court as "Court",
            judge_name as "Judge"
            {appt_col},
            {ea} as "Executive Action",
            status as "Status",
            date_filed as "Date Filed",
            docket_number as "Docket #",
            courtlistener_url as cl_url,
            courtlistener_docket_id as cl_id
            {battle_col}
            {appeal_col}
        FROM cases
        ORDER BY date_filed DESC, case_name
        """

    df = query_df(conn, sql)

    if "Court" in df.columns:
        df["Court"] = df["Court"].apply(court_display_name)

    if search:
        mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
        df = df[mask]

    label = "battles" if view_mode == "Battles Only" else "dockets"
    st.write(f"Showing {len(df)} {label}")

    # Add clickable docket link — prefer stored full URL, fall back to ID-only
    df["Docket Link"] = df.apply(
        lambda row: row["cl_url"] if pd.notna(row.get("cl_url")) and row["cl_url"]
        else (f"https://www.courtlistener.com/docket/{int(row['cl_id'])}/" if pd.notna(row.get("cl_id")) else ""),
        axis=1,
    )
    display_cols = [c for c in df.columns if c not in ("cl_id", "cl_url", "battle_id")]
    display_df = df[display_cols]
    event = st.dataframe(display_df, use_container_width=True, hide_index=True,
                         on_select="rerun", selection_mode="single-row",
                         column_config={
                             "Docket Link": st.column_config.LinkColumn("Docket Link", display_text="View")
                         })

    # If user clicked a row in the dataframe, pre-select that case
    clicked_index = None
    if event and event.selection and event.selection.rows:
        clicked_index = event.selection.rows[0]

    # Case detail expander
    if not df.empty:
        case_names = df["Case Name"].tolist()
        default_idx = clicked_index if clicked_index is not None else 0
        selected = st.selectbox("Select a case for details", case_names, index=default_idx)
        if selected:
            case_row = df[df["Case Name"] == selected].iloc[0]
            battle_id = case_row.get("battle_id")

            with st.expander(f"Details: {selected}", expanded=True):
                for col in display_cols:
                    val = case_row.get(col, "")
                    if val and str(val) != "nan" and str(val) != "":
                        st.write(f"**{col}:** {val}")

                # Show related dockets in the same battle
                if has_battle and battle_id and pd.notna(battle_id):
                    appeal_expr = "CASE WHEN is_appeal = 1 THEN 'Appeal' ELSE 'Original' END" if has_appeal else "'Original'"
                    related = query_df(conn, f"""
                        SELECT case_name, docket_number, court, status,
                               {appeal_expr} as type
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

    # Outcome by executive action
    ea = ea_expr(conn)
    sql = f"""
    SELECT
        {ea} as executive_action,
        status,
        COUNT(*) as count
    FROM cases
    WHERE {ea} IS NOT NULL
      AND {ea} != ''
    GROUP BY {ea}, status
    ORDER BY executive_action, count DESC
    """
    df = query_df(conn, sql)

    if not df.empty:
        top_actions = df_ea.head(10)["Executive Action"].tolist()
        filtered = df[df["executive_action"].isin(top_actions)]

        if not filtered.empty:
            # Cluster statuses into meaningful categories
            def categorize_status(status: str) -> str:
                s = status.lower()
                # Order matters — check more specific patterns first
                if any(w in s for w in ["dismissed", "moot", "withdrawn", "terminated", "closed"]):
                    return "Dismissed / Terminated"
                if any(w in s for w in ["appealed", "appeal filed", "cert", "writ"]):
                    return "On Appeal"
                if any(w in s for w in ["upheld", "affirmed", "mandate returned",
                                        "remanded", "overturned", "vacated",
                                        "rehearing", "en banc"]):
                    return "Appellate Decision"
                if any(w in s for w in ["stay granted", "stay entered", "stayed",
                                        "stay and cert", "partial stay",
                                        "abeyance", "postpone"]):
                    return "Stayed"
                if "stay denied" in s or "stay denial" in s or "stay dissolved" in s or "stay found" in s:
                    return "Stay Denied"
                if any(w in s for w in ["pi granted", "tro granted", "injunction granted",
                                        "pi and class", "pi and partial",
                                        "class certified", "enjoined"]):
                    return "Injunction / TRO Granted"
                if any(w in s for w in ["pi denied", "tro denied", "injunction denied",
                                        "tro and pi denied", "pi denial upheld"]):
                    return "Injunction / TRO Denied"
                if "summary judg" in s or "summary judgment" in s:
                    return "Summary Judgment"
                if any(w in s for w in ["suit filed", "complaint filed",
                                        "application filed", "indictment",
                                        "petition", "suit filed"]):
                    return "Pending / Filed"
                if any(w in s for w in ["consolidated", "venue"]):
                    return "Procedural"
                return "Other"

            filtered = filtered.copy()
            filtered["category"] = filtered["status"].apply(categorize_status)

            all_categories = sorted(filtered["category"].unique().tolist())
            selected_categories = st.multiselect(
                "Filter by status category", all_categories, default=all_categories,
                key="ea_status_filter",
            )
            chart_data = filtered[filtered["category"].isin(selected_categories)]

            if not chart_data.empty:
                # Aggregate by category for the chart
                chart_agg = chart_data.groupby(
                    ["executive_action", "category"], as_index=False
                )["count"].sum()

                fig = px.bar(
                    chart_agg, x="executive_action", y="count", color="category",
                    title="Status Breakdown by Executive Action (top 10)",
                    labels={"count": "Dockets", "executive_action": "", "category": "Status Category"},
                    color_discrete_map={
                        "Pending / Filed": "#636EFA",
                        "Injunction / TRO Granted": "#00CC96",
                        "Injunction / TRO Denied": "#EF553B",
                        "Stayed": "#FFA15A",
                        "Stay Denied": "#FF6692",
                        "On Appeal": "#AB63FA",
                        "Appellate Decision": "#19D3F3",
                        "Dismissed / Terminated": "#B6E880",
                        "Summary Judgment": "#FECB52",
                        "Procedural": "#72B7B2",
                        "Other": "#999999",
                    },
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
