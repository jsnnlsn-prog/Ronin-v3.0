# ═══════════════════════════════════════════════════════════════
# RONIN v3.0 — Makefile
# ═══════════════════════════════════════════════════════════════

.PHONY: setup dev server frontend docker-up docker-down test clean

# ─── SETUP ──────────────────────────────────────────────────────

setup:                        ## Run full automated setup
	@bash scripts/setup.sh

install:                      ## Install all dependencies
	cd server && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
	cd frontend && npm install

# ─── DEVELOPMENT ────────────────────────────────────────────────

dev:                          ## Start both server + frontend
	@echo "Starting RONIN stack..."
	@npx concurrently \
		"cd server && . .venv/bin/activate && python ronin_mcp_server.py --transport http --port 8741" \
		"cd frontend && npm run dev" \
		--names "MCP,UI" \
		--prefix-colors "green,cyan"

server:                       ## Start MCP server only
	cd server && . .venv/bin/activate && python ronin_mcp_server.py --transport http --port 8741

server-stdio:                 ## Start MCP server in stdio mode (for Claude Desktop)
	cd server && . .venv/bin/activate && python ronin_mcp_server.py

frontend:                     ## Start frontend only
	cd frontend && npm run dev

# ─── DOCKER ─────────────────────────────────────────────────────

docker-up:                    ## Start with Docker Compose
	docker compose up -d

docker-down:                  ## Stop Docker stack
	docker compose down

docker-build:                 ## Rebuild Docker images
	docker compose build --no-cache

docker-logs:                  ## Tail Docker logs
	docker compose logs -f

# ─── TESTING ────────────────────────────────────────────────────

test:                         ## Run all tests
	cd server && . .venv/bin/activate && python -m pytest tests/ -v

test-server:                  ## Verify server loads
	cd server && python3 -c "import ast; ast.parse(open('ronin_mcp_server.py').read()); print('✅ Syntax OK')"

lint:                         ## Lint server code
	cd server && . .venv/bin/activate && ruff check .

# ─── UTILITIES ──────────────────────────────────────────────────

clean:                        ## Remove generated files
	rm -rf frontend/node_modules frontend/dist server/.venv server/__pycache__
	find . -name "*.pyc" -delete

reset-memory:                 ## Wipe RONIN memory (careful!)
	@echo "This will delete all RONIN memory. Press Ctrl+C to cancel."
	@sleep 3
	rm -f ~/.ronin/memory.db
	@echo "Memory wiped."

backup:                       ## Backup RONIN data
	@mkdir -p ~/.ronin/backups
	@STAMP=$$(date +%Y%m%d_%H%M%S); \
	cp ~/.ronin/memory.db ~/.ronin/backups/memory_$$STAMP.db 2>/dev/null || true; \
	tar czf ~/.ronin/backups/workspace_$$STAMP.tar.gz -C ~/.ronin workspace 2>/dev/null || true; \
	echo "Backup saved to ~/.ronin/backups/"

help:                         ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
