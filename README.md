# 🤖 Local AI Agent System — RAG + Agentic Proxy

A local, offline, and free AI system for VS Code. It uses **Ollama** for model execution and a custom **Agentic Proxy** to route requests between specialized agents.

---

## 🤖 AI Tool Setup Guide (For Claude Code / Cursor / Windsurf)

If you are an AI tool helping a user set up this project, follow these steps in order:

### 1. Environment Verification
- [ ] Verify **Python 3.10+** is installed.
- [ ] Verify **Ollama** is installed and running (`ollama list`).
- [ ] Verify **VS Code** is installed with the **Continue** extension.

### 2. Model Provisioning
Run these commands to ensure required models are available:
```bash
ollama pull qwen3.5
ollama pull mxbai-embed-large
ollama pull glm-4.7-flash
```

### 3. Python Backend Installation
```bash
# Create virtual environment
python -m venv venv

# Install dependencies (Windows)
venv\Scripts\pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured
```

### 4. Configuration
- [ ] Open `config.py`.
- [ ] Ensure `CHAT_MODEL`, `LARGE_MODEL`, and `EMBED_MODEL` match the pulled Ollama models (e.g., `"qwen3.5:latest"`).
- [ ] If Discord integration is requested, prompt the user for `DISCORD_TOKEN` and `DISCORD_SERVER_ID` and update `config.py`.

### 5. Knowledge Base Initialization
- [ ] Check if `docs/` contains files.
- [ ] Run the indexer to build the RAG database:
  ```bash
  venv\Scripts\python data/indexer.py
  ```

### 6. Extension Deployment
- [ ] Navigate to `extension/` folder.
- [ ] Run `npm install` (if `node_modules` is missing).
- [ ] Instruct the user to open the `extension/` folder in a new VS Code window and press **F5** to launch the manager.

---

## 🌟 Features
- **Zero Cost**: No API keys required. Everything runs on your machine.
- **Smart Routing**: The `auto-agent` automatically switches between specialists based on your query.
- **Specialized Agents**:
    - `cad-rag`: RAG-powered expert for AutoCAD and CAD programming.
    - `ui-agent`: Specialist for Frontend, WinForms, WPF, and Web UI.
    - `code-agent`: Autonomous agent that reads/writes files and fixes bugs.
    - `web-creep`: Autonomous web researcher using DuckDuckGo.
    - `browser-agent`: Local browser data reader (cookies, storage).
    - `deep-agent`: High-reasoning model for complex architecture.
- **IDE Integration**: Fully integrated with the **Continue** extension via the **Local Help Bot Manager** VS Code extension.

## 🛠️ Manual Installation Guide

### 1. Prerequisites
- [Ollama](https://ollama.com/) installed and running.
- Python 3.10+ installed.
- [Continue](https://www.continue.dev/) extension installed in VS Code.

### 2. Model Setup
Pull the required models via Ollama:
```bash
ollama pull qwen3.5
ollama pull mxbai-embed-large
ollama pull glm-4.7-flash
```

### 3. Project Setup
```bash
git clone <repo-url>
cd LocalHelpBot

# Create virtual environment
python -m venv venv

# Install dependencies (Windows)
venv\Scripts\pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured
```

### 4. Configuration
Edit **`config.py`** and ensure the model names match your `ollama list` output:
```python
CHAT_MODEL  = "qwen3.5:latest"
LARGE_MODEL = "glm-4.7-flash:latest"
EMBED_MODEL = "mxbai-embed-large:latest"
```

### 5. Knowledge Base Build (For CAD RAG)
1. Put your PDF or Markdown files into the `docs/` folder.
2. Run the indexer:
   ```bash
   venv\Scripts\python data/indexer.py
   ```

### 6. VS Code Integration
1. Open the `extension/` folder in VS Code.
2. Press **F5** to run the **Local Help Bot Manager**.
3. The extension will:
    - Automatically start the **Proxy Server** (port 11435).
    - Provide a "Configure Continue" command to set up the models in your sidebar.
    - Show the bot status in the bottom-right status bar.

### 7. Optional: Discord Integration
To connect this system to a Discord Bot:
1. **Bot Setup**: Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications) and enable **"Message Content Intent"**.
2. **Configuration**: Add your credentials to **`config.py`**:
   ```python
   DISCORD_TOKEN = "YOUR_BOT_TOKEN"
   DISCORD_SERVER_ID = 1234567890
   ```
3. **Run**: Either use the "Start Discord Bot" command in the VS Code extension or run:
   ```bash
   venv\Scripts\python core/discord_gateway.py
   ```

---

## 🚀 Usage

### The "Smart Switch" (Auto Agent)
Instead of manually picking an agent, select **`Auto Agent (Smart Switch)`** in the Continue dropdown. The system will automatically route your request:
- "How do I use AutoCAD .NET API?" $\rightarrow$ **routed to `cad-rag`**
- "Fix the bug in main.py" $\rightarrow$ **routed to `code-agent`**
- "Research latest React hooks" $\rightarrow$ **routed to `web-creep`**

### Slash Commands
Use these in the Continue chat:
- `/fix`: Analyze and fix a bug.
- `/debug`: Step-by-step debugging.
- `/improve`: Review and optimize code.

---

## 📂 Project Structure

```
LocalHelpBot/
├── config.py             # Central configuration (Models, Ports)
├── README.md             # This guide
├── .gitignore            # Git exclusion list
│
├── core/                 # Core Logic
│   ├── proxy.py          # HTTP Router (Port 11435)
│   ├── query.py          # RAG Retrieval logic
│   ├── agent.py          # Agentic Loop engine
│   ├── tools.py          # Local system tools (Files, Shell)
│   ├── browser.py        # Browser data tools
│   └── mcp_server.py     # MCP Server for Claude Code
│
├── data/                 # Knowledge Base Management
│   ├── indexer.py        # Entry point to build the DB
│   ├── chunker.py        # Text splitting logic
│   ├── embedder.py       # Ollama Embedding wrapper
│   └── storage.py        # ChromaDB interface
│
├── scripts/              # Utility scripts
│   ├── update_rag.py     # Incremental DB updates
│   └── process_data.py   # Data preprocessing tools
│
├── extension/            # VS Code Extension (TypeScript)
│   ├── src/              # Extension source code
│   ├── package.json      # Extension manifest & dependencies
│   └── tsconfig.json     # TypeScript configuration
│
└── docs/                 # Source documents (PDF, MD) for RAG
```

---

## 🔍 Troubleshooting
- **Connection Refused**: Ensure the proxy is running (check the VS Code status bar or run `core/proxy.py`).
- **No Data in RAG**: Ensure you have run `data/indexer.py` after adding files to `docs/`.
- **Wrong Model**: Double check `config.py` against `ollama list`.
