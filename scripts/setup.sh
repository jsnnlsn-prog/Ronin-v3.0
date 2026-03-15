#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════
# RONIN v3.0 — Automated Setup Script
# Detects your environment and installs everything needed
# ═══════════════════════════════════════════════════════════════════

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

RONIN_HOME="${RONIN_HOME:-$HOME/.ronin}"

header() {
    echo ""
    echo -e "${CYAN}${BOLD}    ╔═══════════════════════════════════╗${NC}"
    echo -e "${CYAN}${BOLD}    ║   RONIN v3.0 — Setup Installer  ║${NC}"
    echo -e "${CYAN}${BOLD}    ║   MCP + Agentic Tool-Use Loop     ║${NC}"
    echo -e "${CYAN}${BOLD}    ╚═══════════════════════════════════╝${NC}"
    echo ""
}

step() { echo -e "\n${GREEN}${BOLD}[$1/7]${NC} ${BOLD}$2${NC}"; }
info() { echo -e "  ${DIM}→${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; exit 1; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }

check_cmd() {
    if command -v "$1" &>/dev/null; then
        ok "$1 found: $(command -v "$1")"
        return 0
    else
        warn "$1 not found"
        return 1
    fi
}

# ─── MAIN ───────────────────────────────────────────────────────

header

# ── Step 1: Check prerequisites ─────────────────────────────────
step 1 "Checking prerequisites"

MISSING=()

check_cmd python3 || MISSING+=("python3")
check_cmd pip3    || check_cmd pip || MISSING+=("pip")
check_cmd node    || MISSING+=("node")
check_cmd npm     || MISSING+=("npm")

# Optional
check_cmd docker  || warn "Docker not found — docker compose won't work (optional)"
check_cmd git     || warn "Git not found (optional)"

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    fail "Missing required tools: ${MISSING[*]}
    
    Install them first:
      macOS:    brew install python node
      Ubuntu:   sudo apt install python3 python3-pip nodejs npm
      Windows:  winget install Python.Python.3.12 OpenJS.NodeJS"
fi

# Check Python version
PYTHON_BIN="${PYTHON_BIN:-python3}"
PY_VER=$($PYTHON_BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ]]; then
    fail "Python 3.11+ required, found $PY_VER"
fi
ok "Python $PY_VER"

NODE_VER=$(node -v | sed 's/v//')
ok "Node $NODE_VER"

# ── Step 2: Create RONIN home directory ────────────────────────
step 2 "Creating RONIN home directory"

mkdir -p "$RONIN_HOME"/{workspace,backups}
ok "RONIN_HOME: $RONIN_HOME"
ok "Workspace:   $RONIN_HOME/workspace"
ok "Memory DB:   $RONIN_HOME/memory.db (created on first run)"

# ── Step 3: Set up environment ──────────────────────────────────
step 3 "Configuring environment"

if [[ ! -f .env ]]; then
    cp .env.example .env
    info "Created .env from template"
    
    # Prompt for API key
    echo ""
    echo -e "  ${YELLOW}${BOLD}Anthropic API key required for full operation.${NC}"
    echo -e "  ${DIM}Get one at: https://console.anthropic.com/${NC}"
    echo ""
    read -rp "  Enter your API key (or press Enter to skip): " API_KEY
    
    if [[ -n "$API_KEY" ]]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|sk-ant-xxxxxxxxxxxxx|$API_KEY|" .env
        else
            sed -i "s|sk-ant-xxxxxxxxxxxxx|$API_KEY|" .env
        fi
        ok "API key saved to .env"
    else
        warn "Skipped — edit .env later to add your key"
    fi
else
    ok ".env already exists"
fi

# ── Step 4: Install Python dependencies ─────────────────────────
step 4 "Installing MCP server dependencies"

cd server

# Create virtual environment
if [[ ! -d ".venv" ]]; then
    $PYTHON_BIN -m venv .venv
    ok "Created virtual environment: server/.venv"
fi

source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
ok "Python packages installed"

# Verify server can load
python -c "import ronin_mcp_server; print('  ✓ Server module verified')" 2>/dev/null || warn "Server module check failed — may need manual fix"

deactivate
cd ..

# ── Step 5: Install frontend dependencies ───────────────────────
step 5 "Installing frontend dependencies"

cd frontend
npm install --silent 2>/dev/null
ok "Node packages installed"
cd ..

# ── Step 6: Verify installation ─────────────────────────────────
step 6 "Verifying installation"

# Check server syntax
$PYTHON_BIN -c "import ast; ast.parse(open('server/ronin_mcp_server.py').read())" && ok "Server: syntax valid" || fail "Server: syntax error"

# Check frontend build capability
[[ -f "frontend/node_modules/.package-lock.json" ]] || [[ -d "frontend/node_modules" ]] && ok "Frontend: node_modules present" || warn "Frontend: node_modules missing"

[[ -f "frontend/src/Ronin.jsx" ]] && ok "Frontend: Ronin component found" || fail "Frontend: Ronin.jsx missing"
[[ -f "frontend/vite.config.js" ]] && ok "Frontend: Vite config found" || fail "Frontend: vite.config.js missing"

# ── Step 7: Print startup instructions ──────────────────────────
step 7 "Ready to launch"

echo ""
echo -e "${CYAN}${BOLD}  ╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}  ║              RONIN is ready to deploy                ║${NC}"
echo -e "${CYAN}${BOLD}  ╚═══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Option A — Local Development (recommended to start):${NC}"
echo ""
echo -e "    ${GREEN}# Terminal 1: Start MCP server${NC}"
echo -e "    cd server && source .venv/bin/activate"
echo -e "    python ronin_mcp_server.py --transport http --port 8741"
echo ""
echo -e "    ${GREEN}# Terminal 2: Start frontend${NC}"
echo -e "    cd frontend && npm run dev"
echo ""
echo -e "    ${GREEN}# Open in browser:${NC}"
echo -e "    http://localhost:5173"
echo ""
echo -e "  ${BOLD}Option B — Docker (production):${NC}"
echo ""
echo -e "    docker compose up -d"
echo -e "    ${DIM}# → MCP server on :8741, Frontend on :5173${NC}"
echo ""
echo -e "  ${BOLD}Option C — MCP Server only (use with Claude Desktop):${NC}"
echo ""
echo -e "    cd server && source .venv/bin/activate"
echo -e "    python ronin_mcp_server.py"
echo -e "    ${DIM}# → Runs in stdio mode for Claude Desktop integration${NC}"
echo ""
echo -e "  ${BOLD}Key paths:${NC}"
echo -e "    Config:    .env"
echo -e "    Memory:    $RONIN_HOME/memory.db"
echo -e "    Workspace: $RONIN_HOME/workspace/"
echo -e "    Audit log: $RONIN_HOME/audit.jsonl"
echo ""
