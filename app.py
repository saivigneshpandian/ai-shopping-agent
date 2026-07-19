"""
Streamlit frontend for Shopping_agent.ipynb.

This file does NOT redefine the tools/agent — it loads and executes the
notebook's own setup cells (imports, tools, agent, memory wiring) once per
server process, then drives the resulting `agent_with_memory` object from
a chat UI. The notebook is the backend; this is only the frontend.
"""

import contextlib
import json
import os
import re
import sys
import traceback
import uuid
from typing import Optional

import streamlit as st

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NOTEBOOK_PATH = os.path.join(BASE_DIR, "Shopping_agent.ipynb")

# Cell id of the last setup cell (builds `agent_with_memory` + `config`).
# Everything after this cell in the notebook is manual testing/demo code
# and must NOT be executed here. If notebook cells are reordered, inserted,
# or this cell is deleted, update SETUP_END_CELL_ID to the id of whichever
# cell now builds `agent_with_memory` — load_backend() raises RuntimeError
# instead of silently running the whole notebook if the id isn't found.
SETUP_END_CELL_ID = "febcc0d5"

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


@contextlib.contextmanager
def _cwd(path):
    """Temporarily switch the working directory for the exec() call below —
    the notebook's DB_path is built from os.getcwd(), so exec needs cwd set
    to BASE_DIR — without leaving a permanent process-wide chdir behind."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@st.cache_resource
def load_backend():
    with open(NOTEBOOK_PATH, "r", encoding="utf-8") as f:
        nb = json.load(f)

    source_chunks = []
    setup_end_found = False
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        source_chunks.append("".join(cell.get("source", [])))
        if cell.get("id") == SETUP_END_CELL_ID:
            setup_end_found = True
            break

    if not setup_end_found:
        raise RuntimeError(
            f"Could not find the setup-end cell (id={SETUP_END_CELL_ID!r}) in "
            f"{NOTEBOOK_PATH}. The notebook's cells were likely reordered, "
            "renumbered, or that cell was deleted — update SETUP_END_CELL_ID "
            "in app.py to the id of the cell that builds `agent_with_memory`. "
            "Refusing to fall back to executing the entire notebook, since "
            "that would also run its demo/test cells."
        )

    namespace = {"__name__": "shopping_agent_backend"}
    with _cwd(BASE_DIR):
        exec(compile("\n".join(source_chunks), NOTEBOOK_PATH, "exec"), namespace)
    return namespace


backend = load_backend()
agent_with_memory = backend["agent_with_memory"]
UPLOAD_DIR = os.path.join(BASE_DIR, "_uploads")


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="Shopping Assistant", page_icon="🛒")
st.title("🛒 Shopping Assistant")
st.caption("Search products, ask about ratings, upload a photo, and check out — all through chat.")

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "display_messages" not in st.session_state:
    st.session_state.display_messages = []

agent_config = {"configurable": {"session_id": st.session_state.session_id}}

with st.sidebar:
    st.subheader("Search by photo")
    uploaded_image = st.file_uploader("Upload a product photo", type=["jpg", "jpeg", "png"])
    if uploaded_image is not None:
        st.image(uploaded_image, caption=uploaded_image.name, width="stretch")
    analyze_clicked = st.button("Analyze image", disabled=uploaded_image is None)

for msg in st.session_state.display_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


def clean_agent_answer(text: str) -> str:
    """The model sometimes wraps numbers in backticks or skips the blank
    line between list entries, which makes Streamlit's markdown renderer
    merge items onto one line and show prices as inline code."""
    text = text.replace("`", "")
    text = re.sub(r"[ \t]*(?=#\d+\.)", "\n\n", text)
    return text.strip()


def run_agent(agent_text: str, display_text: Optional[str] = None) -> str:
    display_text = agent_text if display_text is None else display_text
    st.session_state.display_messages.append({"role": "user", "content": display_text})
    with st.chat_message("user"):
        st.markdown(display_text)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            max_attempts = 3
            answer = None
            for attempt in range(1, max_attempts + 1):
                try:
                    result = agent_with_memory.invoke(
                        {"messages": [{"role": "user", "content": agent_text}]},
                        config=agent_config,
                    )
                    answer = clean_agent_answer(result["messages"][-1].content)
                    break
                except Exception as e:
                    traceback.print_exc()
                    is_malformed_tool_call = "tool_use_failed" in str(e)
                    if is_malformed_tool_call and attempt < max_attempts:
                        continue
                    answer = f"Sorry, something went wrong talking to the model: {e}"
        st.markdown(answer)

    st.session_state.display_messages.append({"role": "assistant", "content": answer})
    return answer


if analyze_clicked and uploaded_image is not None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved_path = os.path.join(UPLOAD_DIR, uploaded_image.name)
    with open(saved_path, "wb") as f:
        f.write(uploaded_image.getbuffer())
    run_agent(
        f"I uploaded a product photo. Here is its file path: {saved_path}",
        display_text="I uploaded a product photo.",
    )

prompt = st.chat_input("What are you looking for?")
if prompt:
    run_agent(prompt)
