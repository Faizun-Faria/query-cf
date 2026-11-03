"""QueryCF's chat app (phase 6): plain-English questions about US consumer finance complaints.

    streamlit run app/streamlit_app.py

Each visitor gets their own conversation, so follow-up questions work. Every answer comes in
the format that fits it (a number, a chart or a short report) with the queries behind it, and
every question is logged, tagged as coming from the app (US-07, US-11, US-13).
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import query_cf from the repo

import streamlit as st  # noqa: E402

from query_cf import present, webapp  # noqa: E402

REPO = "https://github.com/Faizun-Faria/query-cf"
EXAMPLES = (
    "How many credit card complaints did Capital One get in 2024?",
    "How did the relief rate for credit card complaints change each year from 2022 to 2025?",
    "Which 5 states had the most credit card complaints in 2025?",
    "Which bank has the worst complaints?",
)

st.set_page_config(page_title="QueryCF", page_icon=":material/query_stats:")
try:
    webapp.load_secrets(st.secrets)
except Exception:  # no secrets file locally: .env is read instead
    webapp.load_secrets()


@st.cache_resource(show_spinner="Getting the warehouse ready. The first start downloads it.")
def warehouse():
    settings = webapp.load_app_settings()
    return webapp.open_warehouse(webapp.database_path(settings), settings)


try:
    settings = webapp.load_app_settings()
    if "analyst" not in st.session_state:
        st.session_state.analyst = webapp.new_analyst(warehouse())
        st.session_state.answers = []
        st.session_state.session = uuid.uuid4().hex
except webapp.AppError as exc:
    st.error(str(exc))
    st.stop()
analyst = st.session_state.analyst
limits = settings.limits


def show(question: str, answer) -> None:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        st.markdown(answer.text)
        for n, part in enumerate(present.present(answer).parts):
            if part.kind == "number":
                st.metric(part.title, part.value)
            elif part.figure is not None:
                st.plotly_chart(part.figure, key=f"{answer.question_id}-{n}")
        with st.expander("How this was worked out"):
            if not answer.steps:
                st.markdown("No queries were run.")
            for n, step in enumerate(answer.steps, 1):
                summary, sql = present.describe_step(step)
                st.markdown(f"**{n}. {step['tool']}**: {summary}")
                if sql:
                    st.code(sql, language="sql")
                for warning in (step.get("result") or {}).get("warnings", []):
                    st.caption(f"Warning: {warning}")
            st.caption(present.usage_line(answer))


with st.sidebar:
    st.header("QueryCF")
    st.markdown(
        "An AI analyst over 17.5 million US consumer finance complaints published by the CFPB, "
        "from December 2011 to August 2026. It answers only from defined metrics and shows "
        "every query it ran."
    )
    st.caption(
        "Complaints are unverified consumer reports: a count shows what was reported, not what "
        "happened."
    )
    left = max(limits.questions_per_session - len(st.session_state.answers), 0)
    st.markdown(f"**{present.plural(left, 'question')} left in this visit.**")
    st.markdown("Try one of these:")
    clicked = [q for n, q in enumerate(EXAMPLES) if st.button(q, key=f"example-{n}")]
    st.markdown(f"[Code and evaluation results on GitHub]({REPO})")

st.title("Ask about consumer finance complaints")
for question, answer in st.session_state.answers:
    show(question, answer)

typed = st.chat_input(
    "For example: how many complaints did Wells Fargo get in 2025?",
    max_chars=limits.max_question_chars,
)
question = typed or (clicked[0] if clicked else None)
if question:
    asked = len(st.session_state.answers)
    reason = webapp.refusal(question, asked, limits, webapp.spent_today(analyst.log))
    if reason:
        st.warning(reason)
    else:
        with st.spinner("Working it out..."):
            tags = {"source": webapp.SOURCE, "session": st.session_state.session}
            answer = analyst.ask(question, tags=tags)
        st.session_state.answers.append((question, answer))
        show(question, answer)
