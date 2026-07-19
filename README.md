# 🛒 AI Shopping Agent — Multimodal, Tool-Calling

A conversational shopping assistant built with **LangChain** that searches a product catalog, checks customer ratings, manages a cart, places orders, and can even **identify a product from a photo** using vision.

Unlike a simple Q&A bot, this is an **agent that acts** — it decides which tools to call, chains them together, and writes real orders to a database.

---

## What it can do

- **Browse by description** — "show me organic snacks under $10"
- **Filter by rating** — the agent pulls each product's average rating and filters accordingly
- **Search by image** — upload a product photo; a vision model identifies it and feeds the attributes into search
- **Add to cart** — build up a multi-item cart before checking out
- **Place orders** — confirm with "yes" or "order #2" and the agent writes the order to the database
- **Remembers the conversation** — session-scoped memory, so "order the first one" resolves against what it just showed you

---

## How it works

```
User input (text or image)
        ↓
    LangChain Agent  ──→ decides which tool to call
        ↓
┌────────────────┬────────────┬─────────────┬──────────┬───────────────────────┐
│ search_products│ get_rating │ add_to_cart │ checkout │ describe_product_image│
│  (SQL query)   │(reviews API)│ (cart table)│(writes DB)│    (Gemini vision)   │
└────────────────┴────────────┴─────────────┴──────────┴───────────────────────┘
        ↓
   Grounded response → order confirmation
```

### The tools

| Tool | What it does |
|---|---|
| `search_products` | Queries the SQLite catalog by keyword, with optional price and organic filters |
| `get_rating` | Returns average rating and review count for a product |
| `add_to_cart` | Adds a product to a session-scoped cart, so multiple items can be collected before ordering |
| `checkout` | Places an order — writes to the `orders` table and returns a confirmation |
| `describe_product_image` | Sends an image to Gemini vision, returns product attributes for search |

### Models

- **Groq (Llama 3.1)** — agent reasoning and tool selection
- **Google Gemini 2.5 Flash** — image understanding

### Memory

Conversation history is wired through `RunnableWithMessageHistory` with per-session stores, so the agent keeps context across turns. Each Streamlit session gets its own `session_id`, which also scopes the cart.

---

## Tech stack

`Python` · `LangChain` · `Groq / Llama 3.1` · `Google Gemini` · `SQLite` · `Streamlit`

---

## Sample data

`store.db` ships with a working catalog so you can run it immediately:

- **32 products** across multiple categories, with prices and organic flags
- **102 customer reviews** with ratings
- An `orders` table the agent writes to

---

## Setup

```bash
# 1. Install dependencies
pip install langchain langchain-groq langchain-google-genai \
            streamlit python-dotenv requests

# 2. Add your API keys to a .env file
GROQ_API_KEY=your_groq_key
GOOGLE_API_KEY=your_google_key

# 3. Run the Streamlit app
streamlit run app.py

# or explore the agent in the notebook
jupyter notebook Shopping_agent.ipynb
```

---

## Example

**Text search**
```
You:   show me organic products under $8 with good ratings
Agent: #1. Organic Honey (ID:7) — $6.50 ★4.6 — organic
       #2. Organic Green Tea (ID:12) — $7.25 ★4.4 — organic

       Would you like to order one? Just say yes or give me the number.

You:   order number 1
Agent: Order #5 confirmed: Organic Honey — $6.50
```

**Image search** — upload `honey.jpg`, and the agent identifies the product, searches the catalog for matches, and presents rated options.

---

## Design notes

A few things I learned building this:

- **Tool descriptions are prompts.** The agent picks tools based on their docstrings — vague descriptions cause wrong tool calls.
- **The system prompt does the orchestration.** Enforcing a strict flow (search → rate → present → *then* checkout) stops the agent from ordering something before the user confirms.
- **Agents that write to a database need guardrails.** `checkout` is deliberately excluded from the browsing flow so an order only happens on explicit confirmation.

---

## Roadmap

- [x] Multi-item cart instead of single-product checkout
- [x] Conversation memory across turns
- [ ] Add order history and cancellation tools
- [ ] Persist memory across sessions (currently in-memory per session)
- [ ] Swap orchestration to LangGraph for finer control over agent state
