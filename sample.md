# Full-Stack + LangChain/LangGraph Interview Cheat Sheet

---

## 1. Environment Setup (Conda)

```bash
# Create & activate
conda create -n myproject python=3.11 -y
conda activate myproject

# Python deps
pip install fastapi uvicorn langchain langchain-openai langgraph chromadb pydantic

# Node.js side (separate terminal/project)
mkdir node-backend && cd node-backend
npm init -y
npm install express cors mongoose dotenv
```

---

## 2. FastAPI CRUD (Python)

```python
# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- Models ----------
class Item(BaseModel):
    name: str
    description: Optional[str] = None
    price: float

# ---------- In-memory DB ----------
db: dict[int, Item] = {}
counter = 0

# ---------- CRUD ----------
@app.post("/items")
def create(item: Item):
    global counter
    counter += 1
    db[counter] = item
    return {"id": counter, **item.model_dump()}

@app.get("/items")
def read_all():
    return [{"id": k, **v.model_dump()} for k, v in db.items()]

@app.get("/items/{id}")
def read_one(id: int):
    if id not in db:
        raise HTTPException(404, "Not found")
    return {"id": id, **db[id].model_dump()}

@app.put("/items/{id}")
def update(id: int, item: Item):
    if id not in db:
        raise HTTPException(404, "Not found")
    db[id] = item
    return {"id": id, **item.model_dump()}

@app.delete("/items/{id}")
def delete(id: int):
    if id not in db:
        raise HTTPException(404, "Not found")
    del db[id]
    return {"deleted": id}

# ---------- Run ----------
# uvicorn main:app --reload --port 8000
# Docs at http://localhost:8000/docs
```

### Key Interview Points — FastAPI
- Built on **Starlette** (ASGI) + **Pydantic** (validation)
- Auto-generates **OpenAPI/Swagger** docs
- Async-native: can use `async def` for endpoints
- Dependency injection via `Depends()`
- `HTTPException` for error responses
- Pydantic `BaseModel` auto-validates request body

---

## 3. Node.js/Express CRUD

```javascript
// server.js
const express = require('express');
const cors = require('cors');

const app = express();
app.use(cors());
app.use(express.json()); // parse JSON body

// ---------- In-memory DB ----------
let items = [];
let counter = 0;

// ---------- CRUD ----------
// CREATE
app.post('/items', (req, res) => {
    counter++;
    const item = { id: counter, ...req.body };
    items.push(item);
    res.status(201).json(item);
});

// READ all
app.get('/items', (req, res) => {
    res.json(items);
});

// READ one
app.get('/items/:id', (req, res) => {
    const item = items.find(i => i.id === parseInt(req.params.id));
    if (!item) return res.status(404).json({ error: 'Not found' });
    res.json(item);
});

// UPDATE
app.put('/items/:id', (req, res) => {
    const idx = items.findIndex(i => i.id === parseInt(req.params.id));
    if (idx === -1) return res.status(404).json({ error: 'Not found' });
    items[idx] = { id: items[idx].id, ...req.body };
    res.json(items[idx]);
});

// DELETE
app.delete('/items/:id', (req, res) => {
    const idx = items.findIndex(i => i.id === parseInt(req.params.id));
    if (idx === -1) return res.status(404).json({ error: 'Not found' });
    items.splice(idx, 1);
    res.json({ deleted: parseInt(req.params.id) });
});

// ---------- Run ----------
app.listen(3000, () => console.log('Server on http://localhost:3000'));
// node server.js
```

### Key Interview Points — Express
- `express.json()` middleware parses request body
- `req.params` for URL params, `req.body` for POST/PUT body, `req.query` for query strings
- Middleware pattern: `app.use(fn)` runs before routes
- `res.status(code).json(data)` to respond
- Error handling middleware: `app.use((err, req, res, next) => {})`

---

## 4. Simple Frontend (works with both backends)

```html
<!-- index.html -->
<!DOCTYPE html>
<html>
<body>
  <h2>Items CRUD</h2>
  <input id="name" placeholder="Name" />
  <input id="price" placeholder="Price" type="number" />
  <button onclick="create()">Add</button>
  <div id="list"></div>

  <script>
    // Change to :3000 for Node backend
    const API = 'http://localhost:8000';

    async function create() {
      await fetch(`${API}/items`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: document.getElementById('name').value,
          price: parseFloat(document.getElementById('price').value)
        })
      });
      loadItems();
    }

    async function remove(id) {
      await fetch(`${API}/items/${id}`, { method: 'DELETE' });
      loadItems();
    }

    async function loadItems() {
      const res = await fetch(`${API}/items`);
      const items = await res.json();
      document.getElementById('list').innerHTML = items
        .map(i => `<p>${i.name} - $${i.price} <button onclick="remove(${i.id})">X</button></p>`)
        .join('');
    }

    loadItems();
  </script>
</body>
</html>
```

---

## 5. LangChain Basics (Interview Must-Knows)

### What is LangChain?
A framework for building LLM-powered apps. Core abstractions:

| Concept | What it is |
|---------|-----------|
| **LLM/ChatModel** | Wrapper around OpenAI, Anthropic, etc. |
| **Prompt Template** | Reusable prompt with variables |
| **Chain** | Sequence of steps (prompt → LLM → parse) |
| **Tool** | Function the LLM can call |
| **Agent** | LLM that decides which tools to use |
| **Retriever** | Fetches relevant docs (for RAG) |
| **Memory** | Conversation history management |

### Quick Examples

```python
# Basic chain
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o-mini")
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant."),
    ("human", "{input}")
])

# LCEL (LangChain Expression Language) — pipe syntax
chain = prompt | llm
response = chain.invoke({"input": "What is RAG?"})
print(response.content)
```

```python
# Tool definition
from langchain_core.tools import tool

@tool
def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b

# Bind to LLM
llm_with_tools = llm.bind_tools([multiply])
```

```python
# Output parser
from langchain_core.output_parsers import StrOutputParser

chain = prompt | llm | StrOutputParser()
# Now returns string instead of AIMessage
```

### LCEL (they WILL ask this)
LangChain Expression Language — uses `|` pipe operator to compose chains:
```python
chain = prompt | llm | parser
# Equivalent to: parser(llm(prompt(input)))
```
Every component implements `Runnable` interface: `.invoke()`, `.stream()`, `.batch()`

---

## 6. LangGraph Basics (Interview Must-Knows)

### What is LangGraph?
A library for building **stateful, multi-step LLM workflows** as graphs. Think state machines for AI agents.

### Core Concepts

| Concept | What it is |
|---------|-----------|
| **State** | TypedDict shared across all nodes |
| **Node** | A function that takes state, returns partial state update |
| **Edge** | Connection between nodes (static or conditional) |
| **Conditional Edge** | Routes to different nodes based on state |
| **START / END** | Entry and exit points |
| **Compile** | Turns graph definition into runnable app |

### Why LangGraph over plain LangChain?
- **Cycles** — agents can loop (call tool → check → call again)
- **State persistence** — checkpointing, human-in-the-loop
- **Controllability** — explicit flow, not hidden chain magic
- **Streaming** — token-level streaming built in

### Minimal Example

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

# 1. STATE
class State(TypedDict):
    messages: Annotated[list, add_messages]  # add_messages = append reducer

# 2. NODES
def chatbot(state: State):
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

# 3. GRAPH
graph = StateGraph(State)
graph.add_node("chatbot", chatbot)
graph.add_edge(START, "chatbot")
graph.add_edge("chatbot", END)

# 4. COMPILE & RUN
app = graph.compile()
result = app.invoke({"messages": [("human", "hi")]})
```

### Agent Pattern (the one they always ask)

```python
def should_continue(state):
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:    # LLM wants to use a tool
        return "tools"
    return END                  # LLM is done

graph = StateGraph(State)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
graph.add_edge("tools", "agent")  # CYCLE — this is the key part

app = graph.compile()
```

Flow: `agent → needs tool? → yes → run tool → back to agent → needs tool? → no → END`

### Annotated + Reducers (they might ask)
```python
messages: Annotated[list, add_messages]
```
The `add_messages` reducer means: when a node returns `{"messages": [new_msg]}`, it **appends** instead of replacing. Without it, returning messages would overwrite the whole list.

---

## 7. RAG (Retrieval-Augmented Generation)

### What is RAG?
Instead of relying only on the LLM's training data, you **retrieve relevant documents** and pass them as context. Three steps: **Index → Retrieve → Generate**.

### Implementation

```python
# STEP 1: Index documents
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Split docs into chunks
splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
chunks = splitter.split_text(long_document_text)

# Embed and store
embeddings = OpenAIEmbeddings()
vectorstore = Chroma.from_texts(chunks, embeddings)
retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

# STEP 2: Retrieve
docs = retriever.invoke("What is the refund policy?")

# STEP 3: Generate with context
from langchain_core.prompts import ChatPromptTemplate

rag_prompt = ChatPromptTemplate.from_messages([
    ("system", "Answer based on this context:\n{context}"),
    ("human", "{question}")
])

rag_chain = rag_prompt | llm | StrOutputParser()
answer = rag_chain.invoke({
    "context": "\n".join(d.page_content for d in docs),
    "question": "What is the refund policy?"
})
```

### RAG in LangGraph

```python
def retrieve_node(state: State):
    query = state["messages"][-1].content
    docs = retriever.invoke(query)
    context = "\n".join(d.page_content for d in docs)
    from langchain_core.messages import SystemMessage
    return {"messages": [SystemMessage(content=f"Context:\n{context}")]}

graph = StateGraph(State)
graph.add_node("retrieve", retrieve_node)
graph.add_node("agent", agent_node)
graph.add_edge(START, "retrieve")
graph.add_edge("retrieve", "agent")
graph.add_edge("agent", END)
```

### Key Interview Terms
- **Embedding** — vector representation of text (OpenAI, Cohere, HuggingFace)
- **Vector store** — database for embeddings (Chroma, Pinecone, FAISS, Weaviate)
- **Chunk** — splitting large docs into smaller pieces for better retrieval
- **chunk_size** — how big each piece is (tokens/chars)
- **chunk_overlap** — overlap between pieces to not lose context at boundaries
- **k** — number of docs to retrieve
- **Similarity search** — cosine similarity between query embedding and stored embeddings

---

## 8. Full Stack: FastAPI + LangGraph + RAG + Frontend

```python
# app.py — complete backend
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from typing import TypedDict, Annotated

# --- Setup ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

llm = ChatOpenAI(model="gpt-4o-mini")
embeddings = OpenAIEmbeddings()
vectorstore = Chroma.from_texts(
    ["Your company docs here", "More docs here"],
    embeddings
)
retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

# --- LangGraph ---
class State(TypedDict):
    messages: Annotated[list, add_messages]

def retrieve(state: State):
    query = state["messages"][-1].content
    docs = retriever.invoke(query)
    ctx = "\n".join(d.page_content for d in docs)
    return {"messages": [SystemMessage(content=f"Context:\n{ctx}")]}

def generate(state: State):
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

graph = StateGraph(State)
graph.add_node("retrieve", retrieve)
graph.add_node("generate", generate)
graph.add_edge(START, "retrieve")
graph.add_edge("retrieve", "generate")
graph.add_edge("generate", END)
rag_app = graph.compile()

# --- API ---
class ChatReq(BaseModel):
    message: str

@app.post("/chat")
def chat(req: ChatReq):
    result = rag_app.invoke({"messages": [HumanMessage(content=req.message)]})
    ai_msgs = [m for m in result["messages"] if isinstance(m, AIMessage)]
    return {"reply": ai_msgs[-1].content if ai_msgs else "No response"}
```

```html
<!-- chat.html -->
<!DOCTYPE html>
<html>
<body>
  <h2>RAG Chat</h2>
  <div id="chat" style="border:1px solid #ccc;padding:10px;height:300px;overflow-y:auto;"></div>
  <input id="msg" placeholder="Ask something..." style="width:80%"/>
  <button onclick="send()">Send</button>
  <script>
    async function send() {
      const msg = document.getElementById('msg').value;
      if (!msg) return;
      appendMsg('You', msg);
      document.getElementById('msg').value = '';
      const res = await fetch('http://localhost:8000/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ message: msg })
      });
      const data = await res.json();
      appendMsg('Bot', data.reply);
    }
    function appendMsg(who, text) {
      const chat = document.getElementById('chat');
      chat.innerHTML += `<p><b>${who}:</b> ${text}</p>`;
      chat.scrollTop = chat.scrollHeight;
    }
    document.getElementById('msg').addEventListener('keydown', e => {
      if (e.key === 'Enter') send();
    });
  </script>
</body>
</html>
```

---

## 9. Quick-Fire Interview Q&A

**Q: Difference between LangChain and LangGraph?**
LangChain = chains (linear pipelines). LangGraph = graphs (cycles, conditional routing, state). Use LangGraph when your agent needs to loop or make decisions.

**Q: What's LCEL?**
LangChain Expression Language. Pipe syntax (`prompt | llm | parser`) to compose runnables. Every piece has `.invoke()`, `.stream()`, `.batch()`.

**Q: How does RAG work?**
Index docs as embeddings → user query gets embedded → similarity search finds top-k chunks → chunks injected as context into LLM prompt → LLM generates answer grounded in those docs.

**Q: Why chunk documents?**
LLMs have context limits. Smaller chunks = more precise retrieval. Overlap prevents losing info at boundaries.

**Q: What's a conditional edge in LangGraph?**
A function that inspects state and returns the name of the next node. Used for routing — e.g., "if LLM called a tool, go to tool_node, else go to END."

**Q: What's the agent loop pattern?**
agent → check tool_calls → yes → execute tools → back to agent → check again → no → END. The cycle is what makes it an agent vs a simple chain.

**Q: FastAPI vs Express?**
FastAPI: Python, async-native, auto-validation via Pydantic, auto docs. Express: Node.js, minimal/unopinionated, middleware-based, massive ecosystem.

**Q: What's a vector store?**
A database optimized for storing and querying high-dimensional vectors (embeddings). Supports similarity search. Examples: Chroma, FAISS, Pinecone.

**Q: How do you add memory to a chatbot?**
LangGraph: use checkpointing (`MemorySaver`). LangChain: `ConversationBufferMemory` or just pass full message history in state.