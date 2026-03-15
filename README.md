# RONIN v3.0 -- Semi-Autonomous Super Agent

All 6 phases complete. ~9,000 lines, 40+ files, 188 passing tests, 50+ API endpoints, 12 SQLite tables.

Built by **Dopamine Ronin** (Jason "Jay" Nelson)

```
ronin-stack/
├── frontend/
│   ├── src/Ronin.jsx          # 1,345L -- Full agent UI (React + Vite + shadcn/ui)
│   ├── src/main.jsx
│   ├── index.html
│   ├── Dockerfile
│   ├── package.json
│   └── vite.config.js
│
├── server/
│   ├── api.py                  # 1,554L -- FastAPI REST (50+ endpoints)
│   ├── ronin_mcp_server.py    #   899L -- 13 MCP tools, SQLite DB init
│   ├── model_router.py         #   584L -- Claude + Venice dual routing
│   ├── ttsi.py                 #   502L -- TT-SI pre-flight simulation
│   ├── a2a_protocol.py         #   399L -- Agent-to-agent messaging
│   ├── cli.py                  #   389L -- Terminal client (REPL + single-shot)
│   ├── event_queue.py          #   373L -- Async event bus + dispatch
│   ├── agent_cards.py          #   355L -- Agent registry (6 internal agents)
│   ├── integrations/slack_bot.py # 304L -- Slack bot integration
│   ├── scheduler.py            #   301L -- Cron-based task scheduler
│   ├── auth.py                 #   264L -- JWT auth + user management
│   ├── notifications.py        #   249L -- Multi-channel notification router
│   ├── logging_config.py       #   229L -- Structured JSON logging
│   ├── resilience.py           #   226L -- Rate limiting + circuit breakers
│   ├── token_optimizer.py      #   221L -- 5 token optimization strategies
│   ├── backup.py               #   219L -- SQLite backup/restore/export
│   ├── watchers/system_monitor.py # 214L -- CPU/mem/disk monitoring
│   ├── context_stream.py       #   193L -- Event aggregator for prompts
│   ├── watchers/filesystem.py  #   161L -- File change watcher
│   ├── vault.py                #   151L -- Fernet-encrypted key storage
│   ├── capability_matcher.py   #   115L -- Task-to-agent matching
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tests/                  # 15 test files, 188+ passing
│
├── deploy/
│   ├── Dockerfile.prod         # Production Docker image
│   ├── cloudbuild.yaml         # GCP Cloud Build config
│   └── deploy.sh               # One-command GCP deploy script
│
├── config/
│   ├── claude_desktop_config.json
│   └── vscode_mcp_settings.json
│
├── scripts/setup.sh            # Automated installer
├── docker-compose.yml          # 3 services: MCP, API, Frontend
├── Makefile                    # 14 convenience commands
└── .env.example                # Config template
```

---

## Production Deployment

RONIN is deployed on GCP (pureswarm-fortress project):

| Service | Port | URL |
|---------|------|-----|
| MCP Tool Server | 8741 | http://GCP_IP:8741 |
| REST API | 8742 | http://GCP_IP:8742/api/health |
| Frontend | 5173 | http://GCP_IP:5173 |

### Deploy to GCP

```bash
./deploy/deploy.sh pureswarm-fortress us-central1
```

This handles Artifact Registry, Secret Manager, Cloud Storage, Docker build, and Cloud Run deploy in one command.

---

## Local Development

### Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Python | 3.11+ | python3 --version |
| Node.js | 20+ | node --version |
| Docker | 24+ | docker --version (optional) |

### Quick Start

```bash
# Automated setup (creates venv, installs deps, prompts for API key)
bash scripts/setup.sh

# Or manual:
cd server && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cd ../frontend && npm install
```

### Running Locally

```bash
# Option A: Both services
make dev

# Option B: Separate terminals
cd server && source .venv/bin/activate && python api.py --port 8742
cd frontend && npm run dev

# Option C: Docker
docker compose up -d
```

Frontend: http://localhost:5173
API: http://localhost:8742/api/health
MCP Server: http://localhost:8741

### CLI Client

```bash
cd server

# Interactive REPL
python cli.py

# Single command
python cli.py "list my scheduled tasks"

# Watch live events
python cli.py --watch

# System status
python cli.py --status
```

---

## Architecture

```
+------------------------------------------------------------+
|  INTERFACES                                                 |
|  React UI - CLI - Slack Bot - Voice (Whisper/TTS)           |
+------------------------------------------------------------+
|  REST API (FastAPI -- api.py)                               |
|  50+ endpoints - JWT auth - Rate limiting - Circuit breakers|
+------------------------------------------------------------+
|  INTELLIGENCE LAYER                                         |
|  Model Router (Claude + Venice) - TT-SI Pre-flight          |
|  Token Optimizer (5 strategies) - Capability Matcher         |
+------------------------------------------------------------+
|  AGENT SYSTEM                                               |
|  6 Agents: Cortex - Scout - Forge - Prism - Echo - Aegis    |
|  A2A Protocol - Agent Cards - Task Delegation                |
+------------------------------------------------------------+
|  PROACTIVE INTELLIGENCE                                     |
|  Event Queue - Scheduler (cron) - Filesystem Watcher         |
|  System Monitor - Notification Router - Context Stream       |
+------------------------------------------------------------+
|  MCP TOOL SERVER (13 tools)                                 |
|  Shell - Files - Code Sandbox - Web Fetch - Memory - Safety  |
+------------------------------------------------------------+
|  PERSISTENCE (SQLite -- 12 tables)                          |
|  Semantic Memory - Episodic Memory - KV Store - Audit Log    |
|  Agent Registry - A2A Tasks - Events - Schedules - Users     |
|  Refresh Tokens - Vault - TT-SI Outcomes                     |
+------------------------------------------------------------+
```

---

## Model Router

Requests route between Claude (Anthropic) and Venice AI based on task classification:

| Tier | Provider | Reason |
|------|----------|--------|
| Orchestrator | Claude | Native tool-use, strongest reasoning |
| TT-SI | Claude | Safety simulation not cost-optimized |
| Tool Use | Claude | Multi-turn tool calling reliability |
| Safety | Claude | Aegis evaluations always full-quality |
| Privacy | Venice | Zero data retention for sensitive work |
| Reasoning | Claude | Deep analysis (fallback: Venice DeepSeek-R1) |
| Generation | Venice | Cost-effective content generation |
| Simple | Venice | Cheapest model for formatting/definitions |
| Bulk | Venice | Batch processing optimization |

Venice is optional. Without a Venice key, all requests route to Claude automatically.

---

## Token Optimization (5 Strategies)

1. **Prompt Caching** -- System prompt + tool defs cached at Anthropic (90% savings on ~3K tokens/call)
2. **Dynamic Tool Filtering** -- Only send tools the task tier needs (0-12 tools per call)
3. **Rolling Conversation Compression** -- Summarize old messages, keep last 4 verbatim
4. **Memory Relevance Filtering** -- Jaccard keyword scoring, only inject matching memories
5. **Response Token Budgets** -- Right-size max_tokens per tier (output costs 5x input)

Combined savings: 40-60% token reduction on a typical session. All tracked per-request.

---

## API Endpoints (50+)

### Core

```
GET  /api/health                    System status + resource metrics
GET  /api/tools                     List MCP tools with schemas
POST /api/tools/{name}              Execute a tool (auth required)
POST /api/batch                     Execute multiple tools (auth required)
```

### Memory + Conversations

```
GET  /api/memory/semantic           List semantic memories
GET  /api/memory/episodic           List episodic memories
POST /api/conversations             Save conversation
GET  /api/conversations             List conversations
GET  /api/conversations/{id}        Get conversation
DELETE /api/conversations/{id}      Delete conversation
```

### Agents + A2A

```
GET  /.well-known/agent.json        A2A system card
GET  /api/agents                    List all agents
POST /api/agents                    Register external agent
DELETE /api/agents/{name}           Unregister agent
POST /api/agents/match              Find best agent for task
POST /a2a/tasks/send                Send task to agent
GET  /a2a/tasks/{id}                Get task status
```

### Proactive Intelligence

```
POST /api/webhooks/{source}         Receive external webhooks
GET/POST/PUT/DELETE /api/schedules  Schedule CRUD
POST /api/schedules/{id}/run        Trigger schedule now
GET/PUT /api/notifications/config   Notification settings
GET  /api/context                   Context stream summary
GET  /api/events                    Recent events
```

### Auth + Security

```
POST /api/auth/register             Create user
POST /api/auth/login                Get JWT token
POST /api/auth/refresh              Refresh token
GET  /api/auth/me                   Current user info
GET/PUT/DELETE /api/vault/{name}    Encrypted key management
GET  /api/metrics                   Request metrics
GET  /api/ttsi/stats                TT-SI accuracy stats
POST /api/backups                   Create DB backup
GET  /api/export                    Export all user data
```

### Voice + CLI + Slack

```
GET  /api/voice/status              Voice availability check
POST /api/voice/transcribe          Audio to text (Whisper)
POST /api/voice/synthesize          Text to audio (TTS-1)
POST /api/cli/run                   Single-shot agentic loop
POST /api/slack/command             Slack slash command handler
```

---

## Test Suite

188 passing tests across 15 test files. 3 pre-existing failures (bcrypt/passlib compatibility in Python 3.12 -- not a code defect).

```bash
cd server
source .venv/bin/activate
python -m pytest tests/ -v
```

---

## Integration

### Claude Desktop

Copy config/claude_desktop_config.json to:
- macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
- Windows: %APPDATA%\Claude\claude_desktop_config.json

Edit the cwd path, restart Claude Desktop. All 13 MCP tools appear in the tool menu.

### VS Code (Copilot)

Add config/vscode_mcp_settings.json content to your VS Code settings.json.

### Slack

1. Store SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET in the vault
2. Point Slack event URL to /api/webhooks/slack
3. RONIN responds to @mentions and /ronin slash commands

---

## Makefile

```bash
make help           # Show all commands
make setup          # Automated setup
make dev            # Start API + frontend
make server         # API server only
make frontend       # Frontend only
make docker-up      # Docker Compose start
make docker-down    # Docker Compose stop
make test           # Run all tests
make backup         # Backup memory + workspace
make reset-memory   # Wipe memory DB
make clean          # Remove deps/cache
```

---

## Key Technical Decisions

- **get_current_user** reads from request.app.state.db (not a fresh connection) to see uncommitted writes
- **get_fresh_db()** returns the shared connection to avoid SQLite inter-connection lock contention
- **bcrypt pinned at 4.0.1** for passlib compatibility
- **TIER_TOOL_MAP** (backend) and **TIER_TOOLS** (frontend) must stay in sync when tools change
- **Tests use monkeypatch** to isolate SQLite DBs per test (prevents lock contention)
- **TT-SI fails open** -- if the pre-flight check errors, execution proceeds with caution rather than blocking
