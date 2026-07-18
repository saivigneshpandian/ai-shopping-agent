import base64
import json
import os
import sqlite3
from typing import Optional

import streamlit as st
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from reviews_api import get_product_rating

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_path = os.path.join(BASE_DIR, "store.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "_uploads")

llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0)
vision_llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")


@tool
def search_products(query: str, max_price: Optional[float] = None, is_organic: Optional[bool] = None) -> str:
    """
    Search the product database by the keyword (matched against name, description and category).
    Optionally filter by maximum price and/or organic status.
    Return a JSON array of matching products, each with: id, name, category, price, description, is_organic.
    """
    conn = sqlite3.connect(DB_path)
    cursor = conn.cursor()

    sql = "SELECT id,name,category,price,description,is_organic FROM products WHERE 1=1"
    params: list = []

    if query:
        sql += " AND name LIKE ?"
        params.append(f"%{query}%")

    if max_price is not None:
        sql += " AND price <= ?"
        params.append(max_price)

    if is_organic is not None:
        sql += " AND is_organic = ?"
        params.append(1 if is_organic else 0)

    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()

    products = [
        {
            "id": row[0],
            "name": row[1],
            "category": row[2],
            "price": row[3],
            "description": row[4],
            "is_organic": bool(row[5]),
        }
        for row in rows
    ]
    return json.dumps(products)


@tool
def get_rating(product_id: int) -> str:
    """
    Get the average customer rating and total review count for a product by its ID. Returns a JSON object
    with: product_id, average_rating, review_count.
    """
    result = get_product_rating(product_id)
    return json.dumps(result)


@tool
def checkout(product_id: int) -> str:
    """
    Place an order for the given product ID. Saves the order to the database and returns a confirmation message
    with the order ID, product name, and price.
    """
    conn = sqlite3.connect(DB_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name, price FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return f"Error: product with ID {product_id} not found."

    name, price = row
    cursor.execute(
        "INSERT INTO orders (product_id, product_name, price) VALUES (?, ?, ?)",
        (product_id, name, price),
    )
    order_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return (
        f"Order #{order_id} confirmed! '{name}' has been successfully ordered for ${price:.2f}. "
        f"Your order will arrive in 3-5 business days. Thank you for shopping with us!"
    )


@tool
def describe_product_image(image_path: str) -> str:
    """
    Analyze a product image and return its key attributes as a JSON object. Use this when the user
    uploads a photo of a product they are interested in. The returned attributes can be used directly
    with search_products.
    """
    if image_path.startswith("http://") or image_path.startswith("https://"):
        import requests
        image_data = base64.b64encode(requests.get(image_path).content).decode()
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    else:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"

    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": (
                    "Look at this product image and extract its key attributes. "
                    "Return ONLY a JSON object with these fields:\n"
                    "- product_type: what kind of product it is (e.g. honey, olive oil)\n"
                    "- search_query: a short keyword to search for it\n"
                    "- is_organic: true if the label says organic, false if not, null if unclear\n"
                    "- description: one sentence describing the product"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_data}"},
            },
        ]
    )

    response = vision_llm.invoke([message])
    return response.content


agent = create_agent(
    tools=[search_products, get_rating, checkout, describe_product_image],
    model=llm,
    system_prompt=(
        "You are a helpful shopping assistant. Follow these rules strictly.\n\n"
        "IMAGE SEARCH — when the user provides an image path:\n"
        "1. Call describe_product_image with the path to identify the product.\n"
        "2. Use the returned search_query and is_organic to call search_products.\n"
        "3. Continue with the BROWSING flow from step 2 onwards.\n\n"
        "BROWSING — when the user describes what they want to buy:\n"
        "1. Call search_products to find matching items (apply any price/organic filters given).\n"
        "2. For each candidate, call get_rating to retrieve its average rating.\n"
        "3. Filter by the user's minimum rating if specified.\n"
        "4. Present qualifying products as a numbered list. For each item use this exact format "
        "   (plain text, no backticks, no code blocks, no bold, no italic):\n\n"
        "   #<number>. <name> (ID:<product_id>) — $<price> ★<rating> — <organic or non-organic>\n\n"
        "   Add a blank line between each product entry for readability. "
        "   Always include (ID:X) so you can reference it later.\n"
        "5. If only one product qualifies, still show it in the list and ask: "
        "   'Would you like to order it? Just say yes or give me the number.'\n"
        "6. Do NOT call checkout at this stage.\n\n"
        "ORDERING — when the user confirms they want to buy (e.g. 'yes', 'sure', 'go ahead', "
        "'order number 2', 'the first one', 'get me #3'):\n"
        "1. Look at your previous message to find the (ID:X) for the chosen product "
        "   (if only one was listed and the user says 'yes', use that product's ID).\n"
        "2. Call checkout with that product_id (the number from (ID:X)).\n"
        "3. Confirm the order to the user in plain text.\n\n"
        "Never place an order unless the user explicitly confirms. "
        "Never guess a product_id — always take it from the (ID:X) in your own previous message."
    ),
)


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="Shopping Assistant", page_icon="🛒")
st.title("🛒 Shopping Assistant")
st.caption("Search products, ask about ratings, upload a photo, and check out — all through chat.")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "display_messages" not in st.session_state:
    st.session_state.display_messages = []

with st.sidebar:
    st.subheader("Search by photo")
    uploaded_image = st.file_uploader("Upload a product photo", type=["jpg", "jpeg", "png"])
    if uploaded_image is not None:
        st.image(uploaded_image, caption=uploaded_image.name, width="stretch")
    analyze_clicked = st.button("Analyze image", disabled=uploaded_image is None)

for msg in st.session_state.display_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


def run_agent(agent_text: str, display_text: Optional[str] = None) -> str:
    display_text = agent_text if display_text is None else display_text
    st.session_state.messages.append({"role": "user", "content": agent_text})
    st.session_state.display_messages.append({"role": "user", "content": display_text})
    with st.chat_message("user"):
        st.markdown(display_text)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                result = agent.invoke({"messages": st.session_state.messages})
                answer = result["messages"][-1].content
            except Exception as e:
                answer = f"Sorry, something went wrong talking to the model: {e}"
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
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
