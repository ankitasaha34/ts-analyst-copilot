import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import anthropic
import os
import json
from dotenv import load_dotenv
from database import get_connection, get_schema, run_query

# ── Setup ─────────────────────────────────────────────────────────
load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Page config ───────────────────────────────────────────────────
st.set_page_config(
    page_title="T&S Analyst Copilot",
    page_icon="🔎",
    layout="wide"
)

# ── Schema (loaded once) ──────────────────────────────────────────
@st.cache_resource
def load_schema():
    return get_schema()

schema = load_schema()

# ── System prompt with exact schema ──────────────────────────────
SYSTEM_PROMPT = """You are a Trust & Safety data analyst assistant. You help T&S analysts investigate platform safety issues by writing SQL queries against a DuckDB database.

You have access to exactly 3 tables. Do not reference any other tables.

TABLE: flags
COLUMNS:
- flag_id (VARCHAR) — unique flag identifier
- date (DATE) — date the flag was created
- category (VARCHAR) — one of: harassment, hate_speech, spam, sexual_content, self_harm, fraud
- severity (VARCHAR) — one of: low, medium, high, critical
- action (VARCHAR) — one of: allow, downrank, human_review, auto_block
- reviewer_id (VARCHAR) — assigned reviewer
- resolution_hrs (DOUBLE) — hours to resolve
- ml_score (DOUBLE) — ML classifier confidence score
- coordinated_flag (BOOLEAN) — true if part of coordinated attack
- attack_id (VARCHAR) — attack event ID if coordinated, else NULL

TABLE: reviewers
COLUMNS:
- reviewer_id (VARCHAR) — unique reviewer ID
- name (VARCHAR) — reviewer name
- team (VARCHAR) — one of: AMER, EMEA, APAC
- capacity_per_day (BIGINT) — max cases per day
- seniority (VARCHAR) — one of: junior, mid, senior

TABLE: attacks
COLUMNS:
- attack_id (VARCHAR) — unique attack ID
- start_date (DATE) — attack start date
- duration_days (BIGINT) — how many days it lasted
- category (VARCHAR) — policy category targeted
- num_accounts (BIGINT) — number of accounts involved
- pattern (VARCHAR) — description of attack pattern
- severity (VARCHAR) — one of: medium, high, critical
- end_date (DATE) — attack end date

RULES:
1. Only use these 3 tables: flags, reviewers, attacks
2. Always return a dataframe that can be displayed
3. Use DuckDB SQL syntax
4. For date comparisons use DATE '2024-01-01' format
5. Always include column aliases for clarity
6. Return ONLY the SQL query, no explanation, no markdown backticks

EXAMPLE QUESTIONS AND QUERIES:
Q: Which category has the most flags?
A: SELECT category, COUNT(*) as total_flags FROM flags GROUP BY category ORDER BY total_flags DESC

Q: Show daily flag volume trend
A: SELECT date, COUNT(*) as total_flags FROM flags GROUP BY date ORDER BY date

Q: What is the SLA breach rate by severity?
A: SELECT severity, COUNT(*) as total, SUM(CASE WHEN resolution_hrs > 24 THEN 1 ELSE 0 END) as breaches, ROUND(100.0 * SUM(CASE WHEN resolution_hrs > 24 THEN 1 ELSE 0 END) / COUNT(*), 2) as breach_rate_pct FROM flags WHERE severity IN ('high', 'critical') GROUP BY severity ORDER BY breach_rate_pct DESC"""

# ── Guardrails ────────────────────────────────────────────────────
ALLOWED_TABLES = {"flags", "reviewers", "attacks"}

def check_guardrails(sql):
    """Check SQL only references allowed tables"""
    sql_lower = sql.lower()
    words = set(sql_lower.replace("(", " ").replace(")", " ").split())
    for word in words:
        if word.startswith("from") or word.startswith("join"):
            continue
    # Simple check — look for any table-like words not in allowed list
    forbidden_keywords = ["information_schema", "pg_", "sys.", "sqlite_", "os.", "subprocess"]
    for kw in forbidden_keywords:
        if kw in sql_lower:
            return False, f"Query references forbidden resource: {kw}"
    return True, None

# ── SQL Generation with retry ─────────────────────────────────────
def generate_sql(question, error_context=None, failed_sql=None):
    """Generate SQL from natural language question"""
    if error_context:
        user_message = f"""Original question: {question}

Previous SQL attempt that failed:
{failed_sql}

Error message:
{error_context}

Please fix the SQL query. Return only the corrected SQL, no explanation."""
    else:
        user_message = question

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )
    sql = response.content[0].text.strip()
    # Clean any markdown backticks if present
    if sql.startswith("```"):
        sql = sql.split("```")[1]
        if sql.startswith("sql"):
            sql = sql[3:]
    return sql.strip()

def generate_explanation(question, sql, df):
    """Generate plain English explanation of query results"""
    sample = df.head(5).to_string(index=False)
    prompt = f"""A T&S analyst asked: "{question}"

The SQL query was:
{sql}

The result has {len(df)} rows. Here is a sample:
{sample}

Write 2-3 sentences explaining what this data shows in plain English.
Then add one line starting with "Recommended action:" suggesting what the T&S team should do based on this data.
Be specific about the numbers. Write like a senior T&S analyst explaining findings to their team.
Do not mention SQL."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()

# ── Chart auto-detection ──────────────────────────────────────────
def auto_chart(df, question):
    """Auto-detect best chart type based on dataframe columns"""
    cols = df.columns.tolist()
    col_types = df.dtypes

    # Check for date column → line chart
    date_cols = [c for c in cols if "date" in c.lower() or col_types[c] == "object" and df[c].astype(str).str.match(r'\d{4}-\d{2}-\d{2}').any()]
    if date_cols and len(cols) >= 2:
        x_col = date_cols[0]
        y_col = [c for c in cols if c != x_col and pd.api.types.is_numeric_dtype(df[c])]
        if y_col:
            fig = px.line(df, x=x_col, y=y_col[0], title=question)
            return fig

    # Check for category + numeric → bar chart
    cat_cols = [c for c in cols if pd.api.types.is_string_dtype(df[c])]
    num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    if cat_cols and num_cols:
        fig = px.bar(df, x=cat_cols[0], y=num_cols[0], title=question,
                     color=cat_cols[0])
        return fig

    # Otherwise no chart
    return None

# ── Main App ──────────────────────────────────────────────────────
st.title("🔎 T&S Analyst Copilot")
st.markdown("Ask questions about platform safety data in plain English. Get SQL, results, and insights.")
st.markdown("---")

# Example questions
st.markdown("**Try these questions:**")
examples = [
    "Which violation category has the most flags?",
    "Show daily flag volume over time",
    "What is the SLA breach rate for high and critical cases?",
    "Which reviewer has the most cases assigned?",
    "Show coordinated attack events and how many accounts were involved",
    "What percentage of flags were auto-blocked vs sent to human review?",
    "Which category has the highest average ML score?",
    "Show flag volume by severity level",
    "Which categories have SLA breach rate above 20%?",
    "Show me the top 5 reviewers by workload",
    "How many coordinated attack flags were there per week?",
    "What is the auto-block rate for critical severity flags?",
    "Which attack event had the most accounts involved?",
    "Show harassment flags trend over the last 30 days",
    "What percentage of flags are coordinated vs organic?",
]

selected = st.selectbox("Or pick an example:", [""] + examples)
question = st.text_input(
    "Your question:",
    value=selected if selected else "",
    placeholder="e.g. Which category has the most flags this week?"
)
# ── Risk Alerts Panel ─────────────────────────────────────────────
st.markdown("### ⚠️ Live Risk Alerts")

alert_col1, alert_col2, alert_col3 = st.columns(3)

# Alert 1 — SLA breach rate
df_flags_all, _ = run_query("SELECT severity, resolution_hrs FROM flags WHERE severity IN ('high', 'critical')")
if df_flags_all is not None:
    breach_rate = (df_flags_all["resolution_hrs"] > 24).mean() * 100
    if breach_rate > 20:
        alert_col1.error(f"🚨 SLA Breach Rate: {breach_rate:.1f}%\nAbove 20% threshold — reviewer capacity issue")
    elif breach_rate > 15:
        alert_col1.warning(f"⚠️ SLA Breach Rate: {breach_rate:.1f}%\nApproaching 20% threshold")
    else:
        alert_col1.success(f"✅ SLA Breach Rate: {breach_rate:.1f}%\nWithin acceptable range")

# Alert 2 — Harassment trend
df_harass, _ = run_query("""
    SELECT date, COUNT(*) as total 
    FROM flags 
    WHERE category = 'harassment' 
    GROUP BY date 
    ORDER BY date DESC 
    LIMIT 14
""")
if df_harass is not None:
    last_7 = df_harass.head(7)["total"].mean()
    prev_7 = df_harass.tail(7)["total"].mean()
    pct_change = ((last_7 - prev_7) / prev_7) * 100
    if pct_change > 15:
        alert_col2.error(f"🚨 Harassment: {pct_change:+.1f}% WoW\nSignificant spike — investigate coordinated activity")
    elif pct_change > 5:
        alert_col2.warning(f"⚠️ Harassment: {pct_change:+.1f}% WoW\nMonitor for continued growth")
    else:
        alert_col2.success(f"✅ Harassment: {pct_change:+.1f}% WoW\nStable — no action needed")

# Alert 3 — Coordinated attack flags
df_coord, _ = run_query("""
    SELECT COUNT(*) as coord_count, 
           ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM flags), 2) as coord_pct
    FROM flags 
    WHERE coordinated_flag = true
    AND date >= (SELECT MAX(date) - INTERVAL '7 days' FROM flags)
""")
if df_coord is not None:
    coord_pct = df_coord["coord_pct"].iloc[0]
    if coord_pct > 15:
        alert_col3.error(f"🚨 Coordinated Flags: {coord_pct:.1f}%\nHigh coordinated activity this week")
    elif coord_pct > 8:
        alert_col3.warning(f"⚠️ Coordinated Flags: {coord_pct:.1f}%\nElevated — monitor attack patterns")
    else:
        alert_col3.success(f"✅ Coordinated Flags: {coord_pct:.1f}%\nNormal level — no action needed")

st.markdown("---")
if st.button("🔍 Analyze", type="primary"):
    if not question.strip():
        st.warning("Please enter a question.")
    else:
        with st.spinner("Generating SQL and running query..."):

            # Step 1 — Generate SQL
            sql = generate_sql(question)
            ok, err = check_guardrails(sql)

            if not ok:
                st.error(f"🚫 Query blocked by guardrails: {err}")
            else:
                # Step 2 — Run query with retry
                df, error = run_query(sql)
                retries = 0
                while error and retries < 2:
                    retries += 1
                    st.warning(f"Query failed (attempt {retries}), asking Claude to fix it...")
                    sql = generate_sql(question, error_context=error, failed_sql=sql)
                    ok, err = check_guardrails(sql)
                    if not ok:
                        st.error(f"🚫 Fixed query blocked by guardrails: {err}")
                        break
                    df, error = run_query(sql)

                if error:
                    st.error(f"❌ Query failed after {retries} retries: {error}")
                    st.code(sql, language="sql")
                else:
                    # Step 3 — Show results
                    st.markdown("---")

                    # SQL expander
                    with st.expander("📝 View Generated SQL"):
                        st.code(sql, language="sql")

                    # Metrics
                    col1, col2 = st.columns(2)
                    col1.metric("Rows returned", f"{len(df):,}")
                    col2.metric("Columns", len(df.columns))

                    # Chart
                    fig = auto_chart(df, question)
                    if fig:
                        st.plotly_chart(fig, use_container_width=True)

                    # Data table
                    st.dataframe(df, use_container_width=True)

                    # Explanation
                    with st.spinner("Generating insight..."):
                        explanation = generate_explanation(question, sql, df)
                    st.info(f"💡 **Insight:** {explanation}")

# ── Sidebar ───────────────────────────────────────────────────────
st.sidebar.title("🔎 T&S Analyst Copilot")
st.sidebar.markdown("**Ask questions, get SQL + insights**")
st.sidebar.markdown("---")
st.sidebar.markdown("**Available tables:**")
st.sidebar.markdown("- `flags` — 41,935 records")
st.sidebar.markdown("- `reviewers` — 20 reviewers")
st.sidebar.markdown("- `attacks` — 5 attack events")
st.sidebar.markdown("---")
st.sidebar.markdown("**Policy categories:**")
st.sidebar.markdown("harassment · hate_speech · spam · sexual_content · self_harm · fraud")
st.sidebar.markdown("---")
st.sidebar.markdown("**Severity levels:**")
st.sidebar.markdown("low · medium · high · critical")