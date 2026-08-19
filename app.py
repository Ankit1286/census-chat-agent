import streamlit as st
from anthropic import Anthropic

from agent.guardrails import OFF_TOPIC_REPLY, UNSAFE_REPLY, classify_message
from agent.llm_client import AgentError, run_agent_turn
from agent.tools import SnowflakeUnavailableError, get_connection

st.set_page_config(page_title="US Census Chat", page_icon="\U0001F4CA")
st.title("\U0001F4CA US Census Population Chat")
st.caption(
    "Ask questions about US population and demographics, grounded in the US Open Census "
    "Data available on the Snowflake Marketplace. If a question can't be answered from this "
    "dataset, I'll say so rather than guess."
)

MAX_MESSAGES_PER_SESSION = 40

if "messages" not in st.session_state:
    st.session_state.messages = []  # Anthropic-format messages, including tool turns
if "display_messages" not in st.session_state:
    st.session_state.display_messages = []  # (role, text) pairs for rendering only


@st.cache_resource
def get_anthropic_client() -> Anthropic:
    return Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])


def get_snowflake_connection():
    if "sf_conn" not in st.session_state:
        st.session_state.sf_conn = get_connection(st.secrets)
    return st.session_state.sf_conn


for role, text in st.session_state.display_messages:
    with st.chat_message(role):
        st.markdown(text)

user_input = st.chat_input("Ask about US population...")

if user_input:
    if len(st.session_state.display_messages) >= MAX_MESSAGES_PER_SESSION:
        st.warning(
            "This session has reached its message limit. Please refresh the page to start a "
            "new conversation."
        )
        st.stop()

    st.session_state.display_messages.append(("user", user_input))
    with st.chat_message("user"):
        st.markdown(user_input)

    client = get_anthropic_client()

    with st.chat_message("assistant"):
        classification = classify_message(client, user_input)

        if classification == "UNSAFE":
            reply = UNSAFE_REPLY
            st.markdown(reply)
        elif classification == "OFF_TOPIC":
            reply = OFF_TOPIC_REPLY
            st.markdown(reply)
        else:
            st.session_state.messages.append({"role": "user", "content": user_input})
            try:
                with st.status("Querying Census data...", expanded=False):
                    conn = get_snowflake_connection()
                    reply, st.session_state.messages = run_agent_turn(
                        client, conn, st.session_state.messages
                    )
                st.markdown(reply)
            except SnowflakeUnavailableError:
                reply = (
                    "I'm having trouble connecting to the Census data right now. Please try "
                    "again in a moment."
                )
                st.markdown(reply)
            except AgentError as exc:
                reply = str(exc)
                st.markdown(reply)
            except Exception:
                reply = (
                    "Something went wrong while processing that question. Please try "
                    "rephrasing it, or try again shortly."
                )
                st.markdown(reply)

    st.session_state.display_messages.append(("assistant", reply))
