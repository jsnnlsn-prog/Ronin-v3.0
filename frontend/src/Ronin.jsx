import { useState, useEffect, useRef, useCallback, useReducer } from "react";

// ============================================================================
// RONIN v3.0 — shadcn/ui Design System · High Contrast · Professional
// Full agentic tool-use loop with MCP tools preserved from v2
// ============================================================================

// ─── DESIGN TOKENS (shadcn zinc palette, high contrast) ───────────────────
const T = {
  bg:        "#09090b",   // zinc-950
  bg2:       "#18181b",   // zinc-900
  bg3:       "#27272a",   // zinc-800
  border:    "#3f3f46",   // zinc-700
  border2:   "#52525b",   // zinc-600
  muted:     "#a1a1aa",   // zinc-400
  text:      "#e4e4e7",   // zinc-200
  bright:    "#fafafa",   // zinc-50
  accent:    "#10b981",   // emerald-500
  accent2:   "#34d399",   // emerald-400
  accentDim: "#065f46",   // emerald-900
  blue:      "#3b82f6",
  amber:     "#f59e0b",
  rose:      "#f43f5e",
  violet:    "#8b5cf6",
  cyan:      "#06b6d4",
  orange:    "#f97316",
};

const font = {
  mono: "'Geist Mono', 'JetBrains Mono', 'Fira Code', ui-monospace, monospace",
  sans: "'Geist', 'SF Pro Display', -apple-system, system-ui, sans-serif",
};

// ─── AGENT REGISTRY ────────────────────────────────────────────────────────
const AGENTS = {
  cortex:       { id: "cortex",       name: "Cortex",  role: "Orchestrator",        icon: "🧠", color: T.cyan },
  researcher:   { id: "researcher",   name: "Scout",   role: "Research & Intel",    icon: "🔍", color: T.amber },
  engineer:     { id: "engineer",     name: "Forge",   role: "Code & Systems",      icon: "⚡", color: T.accent },
  analyst:      { id: "analyst",      name: "Prism",   role: "Analysis",            icon: "📊", color: T.rose },
  communicator: { id: "communicator", name: "Echo",    role: "Communication",       icon: "✍️", color: T.violet },
  guardian:     { id: "guardian",     name: "Aegis",   role: "Safety",              icon: "🛡️", color: T.orange },
};

// ─── API CLIENT ────────────────────────────────────────────────────────────
// REST API base URL — points to the FastAPI wrapper around MCP tools.
// In Docker: server:8742, local dev: localhost:8742
const API_BASE = window.__RONIN_API_URL || import.meta.env?.VITE_API_URL || "http://localhost:8742";
const USER_NAME = import.meta.env?.VITE_USER_NAME || "Jay";
const USER_INITIAL = "J";

// Fallback MCP_TOOLS — used until /api/tools responds. Keeps Claude tool-use working immediately.
let MCP_TOOLS = [
  { name: "ronin_shell_exec", description: "Execute a shell command in the sandboxed RONIN workspace.", input_schema: { type: "object", properties: { command: { type: "string", description: "Shell command to execute" }, working_dir: { type: "string" }, timeout: { type: "integer" } }, required: ["command"] } },
  { name: "ronin_code_exec", description: "Execute code in a sandboxed environment. Supports Python, JavaScript, and Bash.", input_schema: { type: "object", properties: { language: { type: "string", enum: ["python", "javascript", "bash"] }, code: { type: "string" }, timeout: { type: "integer" } }, required: ["language", "code"] } },
  { name: "ronin_file_write", description: "Write content to a file in the RONIN workspace.", input_schema: { type: "object", properties: { path: { type: "string" }, content: { type: "string" }, mode: { type: "string", enum: ["write", "append"] } }, required: ["path", "content"] } },
  { name: "ronin_file_read", description: "Read a file from the RONIN workspace.", input_schema: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } },
  { name: "ronin_file_list", description: "List files in the RONIN workspace directory.", input_schema: { type: "object", properties: { directory: { type: "string" }, recursive: { type: "boolean" } } } },
  { name: "ronin_web_fetch", description: "Fetch content from a URL.", input_schema: { type: "object", properties: { url: { type: "string" }, method: { type: "string", enum: ["GET", "POST", "PUT", "DELETE"] }, extract_text: { type: "boolean" } }, required: ["url"] } },
  { name: "ronin_memory_store", description: "Store a fact in RONIN long-term memory. Persists across sessions.", input_schema: { type: "object", properties: { fact: { type: "string" }, confidence: { type: "number" }, tags: { type: "array", items: { type: "string" } } }, required: ["fact"] } },
  { name: "ronin_memory_query", description: "Search RONIN long-term memory for stored facts.", input_schema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } },
  { name: "ronin_episodic_store", description: "Store an interaction in episodic memory with optional reflection.", input_schema: { type: "object", properties: { interaction: { type: "string" }, reflection: { type: "string" }, importance: { type: "number" } }, required: ["interaction"] } },
  { name: "ronin_kv_get", description: "Retrieve a value from the persistent key-value store.", input_schema: { type: "object", properties: { key: { type: "string" } }, required: ["key"] } },
  { name: "ronin_kv_set", description: "Store a key-value pair in persistent storage.", input_schema: { type: "object", properties: { key: { type: "string" }, value: { type: "string" } }, required: ["key", "value"] } },
  { name: "ronin_safety_check", description: "Aegis Guardian: Evaluate an action for safety.", input_schema: { type: "object", properties: { action_description: { type: "string" }, risk_level: { type: "string", enum: ["low", "medium", "high", "critical"] } }, required: ["action_description"] } },
  { name: "ronin_system_info", description: "Get RONIN system status.", input_schema: { type: "object", properties: { component: { type: "string", enum: ["overview", "memory_stats", "audit_recent", "workspace_status"] } } } },
];

// Connection state
let _apiConnected = false;

async function checkApiHealth() {
  try {
    const r = await fetch(`${API_BASE}/api/health`, { signal: AbortSignal.timeout(3000) });
    if (r.ok) { _apiConnected = true; return true; }
  } catch {}
  _apiConnected = false;
  return false;
}

async function fetchToolSchemas() {
  try {
    const r = await fetch(`${API_BASE}/api/tools`);
    if (r.ok) {
      const data = await r.json();
      if (data.tools?.length) MCP_TOOLS = data.tools;
    }
  } catch {}
}

// Agent registry data from API (replaces cosmetic-only AGENTS for status)
let _agentRegistry = []; // Populated from /api/agents

async function fetchAgentRegistry() {
  try {
    const r = await fetch(`${API_BASE}/api/agents`);
    if (r.ok) {
      const data = await r.json();
      _agentRegistry = data.agents || [];
    }
  } catch {}
}

async function loadPersistedMemory(dispatch) {
  try {
    const r = await fetch(`${API_BASE}/api/memory/semantic?limit=100`);
    if (r.ok) {
      const data = await r.json();
      for (const m of (data.memories || [])) {
        dispatch({ type: "MEM_STORE", payload: m });
      }
    }
  } catch {}
}

// Agent name hints based on tool name
const TOOL_AGENTS = {
  ronin_web_fetch: { name: "Scout", color: T.amber },
  ronin_code_exec: { name: "Forge", color: T.accent },
  ronin_shell_exec: { name: "Forge", color: T.accent },
  ronin_file_write: { name: "Forge", color: T.accent },
  ronin_file_read: { name: "Forge", color: T.accent },
  ronin_file_list: { name: "Forge", color: T.accent },
  ronin_memory_store: { name: "Cortex", color: T.cyan },
  ronin_memory_query: { name: "Cortex", color: T.cyan },
  ronin_episodic_store: { name: "Cortex", color: T.cyan },
  ronin_kv_get: { name: "Cortex", color: T.cyan },
  ronin_kv_set: { name: "Cortex", color: T.cyan },
  ronin_safety_check: { name: "Aegis", color: T.orange },
  ronin_system_info: { name: "Cortex", color: T.cyan },
};

// ─── TOOL EXECUTOR ─────────────────────────────────────────────────────────
// Calls the RONIN REST API for real tool execution.
// Falls back to lightweight JS simulation if the API is unreachable.
function createExecutor(dispatch, log) {
  const localFs = {}; // Fallback in-memory filesystem

  return async (name, input) => {
    const agent = TOOL_AGENTS[name] || { name: "Cortex", color: T.cyan };

    // ── Try real API execution first ──
    if (_apiConnected) {
      try {
        log(agent.name, `→ ${name}(${JSON.stringify(input).slice(0, 80)}...)`, agent.color);
        const r = await fetch(`${API_BASE}/api/tools/${name}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input }),
          signal: AbortSignal.timeout(35000), // 30s tool timeout + 5s buffer
        });

        if (!r.ok) {
          const err = await r.text();
          log(agent.name, `✗ HTTP ${r.status}: ${err.slice(0, 100)}`, T.rose);
          return JSON.stringify({ error: `API error: ${r.status}`, details: err.slice(0, 200) });
        }

        const data = await r.json();
        const ms = data.execution_ms ? ` (${data.execution_ms}ms)` : "";

        // Update local state from real results
        if (name === "ronin_memory_store" && data.success) {
          dispatch({ type: "MEM_STORE", payload: { id: data.result?.id || Date.now(), fact: input.fact, confidence: input.confidence || 0.7, tags: input.tags || [] } });
        }
        if (name === "ronin_file_write" && data.success) {
          dispatch({ type: "FILE", payload: { path: input.path, size: data.result?.size || input.content?.length || 0 } });
        }

        log(agent.name, `✓ ${name}${ms}`, agent.color);

        // Return the result as JSON string (what Claude expects as tool_result)
        return JSON.stringify(data.result);

      } catch (e) {
        // Network error — mark disconnected and fall through to simulation
        if (e.name === "TimeoutError" || e.name === "AbortError") {
          log(agent.name, `✗ ${name} timed out`, T.rose);
          return JSON.stringify({ error: "Tool execution timed out" });
        }
        log("Cortex", `API unreachable — falling back to simulation`, T.amber);
        _apiConnected = false;
      }
    }

    // ── Fallback: JS simulation (when API unreachable) ──
    log(agent.name, `[SIM] ${name}`, T.border2);
    switch (name) {
      case "ronin_code_exec":
        if (input.language === "javascript") {
          try {
            const logs = [];
            const sb = { console: { log: (...a) => logs.push(a.map(String).join(" ")), error: (...a) => logs.push("[ERR] " + a.map(String).join(" ")) }, Math, Date, JSON, parseInt, parseFloat, String, Number, Array, Object, Map, Set, RegExp };
            const r = new Function(...Object.keys(sb), input.code)(...Object.values(sb));
            return JSON.stringify({ exit_code: 0, stdout: logs.join("\n") + (r !== undefined ? "\n" + String(r) : "") || "(no output)", stderr: "" });
          } catch (e) { return JSON.stringify({ exit_code: 1, stdout: "", stderr: e.message }); }
        }
        return JSON.stringify({ exit_code: 0, stdout: `[SIMULATED] ${input.language} code received (${input.code?.length || 0} chars). Start RONIN API server for real execution.`, stderr: "" });
      case "ronin_file_write":
        localFs[input.path] = input.mode === "append" ? (localFs[input.path] || "") + input.content : input.content;
        dispatch({ type: "FILE", payload: { path: input.path, size: localFs[input.path].length } });
        return JSON.stringify({ success: true, path: input.path, size: localFs[input.path].length, simulated: true });
      case "ronin_file_read":
        return localFs[input.path] ? JSON.stringify({ path: input.path, content: localFs[input.path].slice(0, 8000), simulated: true }) : JSON.stringify({ error: "File not found (simulation mode)" });
      case "ronin_memory_store":
        dispatch({ type: "MEM_STORE", payload: { id: Date.now(), fact: input.fact, confidence: input.confidence || 0.7, tags: input.tags || [] } });
        return JSON.stringify({ success: true, status: "stored", simulated: true });
      case "ronin_memory_query":
        return JSON.stringify({ query: input.query, results: [], simulated: true });
      case "ronin_safety_check": {
        const d = (input.risk_level === "critical") ? "DENIED" : (input.risk_level === "high") ? "ESCALATE" : "APPROVED";
        return JSON.stringify({ decision: d, risk_level: input.risk_level || "medium", simulated: true });
      }
      case "ronin_system_info":
        return JSON.stringify({ system: "RONIN v3.0", status: "simulation_mode", tools: MCP_TOOLS.length, api_connected: false });
      default:
        return JSON.stringify({ error: `Tool ${name} requires RONIN API server. Run: python api.py`, simulated: true });
    }
  };
}

// ─── SYSTEM PROMPT ─────────────────────────────────────────────────────────
const SYS = `You are RONIN, a semi-autonomous super agent with REAL tools. Use them actively.

AGENTS: Cortex (orchestrator), Scout (research), Forge (engineering), Prism (analysis), Echo (communication), Aegis (safety).

TOOLS: ronin_web_search, ronin_code_exec, ronin_file_write, ronin_file_read, ronin_memory_store, ronin_memory_query, ronin_safety_check, ronin_system_info

PROTOCOL — Structure EVERY response:
[THOUGHT] Analysis of what you know, need, and risks
[PLAN] Steps with assigned agents and tools
[ACTION] Execute tools — be decisive, call multiple if needed
[RESULT] Synthesized outcome
[REFLECTION] What worked, what to remember

RULES:
1. USE TOOLS PROACTIVELY. Don't describe — DO.
2. Write real code with ronin_file_write, test with ronin_code_exec.
3. Store learnings in ronin_memory_store automatically.
4. Be direct, technical, no fluff.`;

// ─── MODEL ROUTER (Client-Side) ───────────────────────────────────────────
// Routes between Claude (Anthropic direct) and Venice AI based on task tier.
// Claude: orchestration, tool-use loops, TT-SI, safety
// Venice: simple completions, drafting, privacy-sensitive, bulk work

const PROVIDERS = {
  claude: { url: "https://api.anthropic.com/v1/messages", format: "anthropic" },
  venice: { url: "https://api.venice.ai/api/v1/chat/completions", format: "openai" },
};

const TIER_KEYWORDS = {
  privacy:    /insurance|client|confidential|medical|hipaa|sensitive|biohazard|private/i,
  safety:     /safety_check|dangerous|delete all|destroy|rm -rf/i,
  tool_use:   /ronin_|execute|run this|shell|deploy/i,
  reasoning:  /analyze|compare|evaluate|trade.?off|architect|debug|diagnose|root cause|why does/i,
  generation: /write|draft|compose|email|blog|proposal|document|letter|summarize|rewrite/i,
  simple:     /format|convert|translate|list|define|what is|how to|explain briefly/i,
};

// Routing table: tier → { provider, model, reason }
const ROUTES = {
  orchestrator: { provider: "gemini", model: "gemini-pro-latest", reason: "Orchestrator needs efficiency & 2M context" },
  ttsi:         { provider: "gemini", model: "gemini-pro-latest", reason: "Gemini reasoning is strong" },
  tool_use:     { provider: "gemini", model: "gemini-pro-latest", reason: "Native function calling" },
  safety:       { provider: "gemini", model: "gemini-pro-latest", reason: "Safety defaults to Gemini" },
  privacy:      { provider: "venice", model: "zai-org-glm-4.7",        reason: "Zero data retention" },
  reasoning:    { provider: "gemini", model: "gemini-pro-latest", reason: "Deep analysis" },
  generation:   { provider: "venice", model: "zai-org-glm-4.7",        reason: "Cost-effective content gen" },
  simple:       { provider: "venice", model: "qwen3-4b",               reason: "Cheapest for simple tasks" },
  default:      { provider: "gemini", model: "gemini-pro-latest", reason: "Default fallback" },
};

function classifyTier(prompt, { hasTools = false, isOrchestrator = false, isTTSI = false } = {}) {
  if (isOrchestrator) return "orchestrator";
  if (isTTSI) return "ttsi";
  if (hasTools) return "tool_use";
  for (const [tier, rx] of Object.entries(TIER_KEYWORDS)) {
    if (rx.test(prompt)) return tier;
  }
  return "default";
}

function routeRequest(prompt, opts = {}) {
  const tier = classifyTier(prompt, opts);
  const route = ROUTES[tier] || ROUTES.default;
  // Fallback to Claude if Venice key not configured
  const veniceAvailable = !!window.__VENICE_KEY;
  if (route.provider === "venice" && !veniceAvailable) {
    return { ...ROUTES.default, tier, fallback: true };
  }
  return { ...route, tier, fallback: false };
}

// ─── AGENTIC LOOP ──────────────────────────────────────────────────────
async function agenticLoop(msgs, sys, tools, exec, onPhase, onLog, maxIter = 6, autonomy = 2) {
  let cur = [...msgs], texts = [], usage = { input_tokens: 0, output_tokens: 0, cache_read: 0, cache_creation: 0 }, toolLog = [], iter = 0;
  let routing = { claude: 0, venice: 0 };

  while (iter < maxIter) {
    iter++;
    onPhase(iter === 1 ? "action" : "observation");

    const route = routeRequest(cur[cur.length - 1]?.content || "", { hasTools: true, isOrchestrator: true });
    onLog("Cortex", `Iter ${iter} → ${route.provider}/${route.model} [${route.tier}]`, T.cyan);
    routing[route.provider] = (routing[route.provider] || 0) + 1;

    let data;
    try {
      const r = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${localStorage.getItem("ronin_token") || ""}`
        },
        body: JSON.stringify({
          messages: cur,
          system: sys,
          provider: route.provider,
          max_tokens: 4096,
          tools: tools.length ? tools.map(t => ({
            name: t.name, description: t.description, input_schema: t.input_schema
          })) : undefined,
          task_hint: cur[cur.length - 1]?.content || "",
          is_orchestrator: true,
        }),
      });
      data = await r.json();
    } catch (e) {
      return { text: `[THOUGHT] Connection error: ${e.message}\n[REFLECTION] System error.`, usage, toolLog, iterations: iter, routing, error: true };
    }

    if (data.error) {
      return {
        text: `[THOUGHT] API error: ${data.error.message}\n[REFLECTION] Check API status.`,
        usage, toolLog, iterations: iter, routing, error: true,
      };
    }

    if (data.usage) {
      usage.input_tokens += data.usage.input_tokens || 0;
      usage.output_tokens += data.usage.output_tokens || 0;
    }

    const tb = (data.content || []).filter(b => b.type === "text");
    const tu = (data.content || []).filter(b => b.type === "tool_use");
    if (tb.length) texts.push(tb.map(b => b.text).join("\n"));
    if (!tu.length || data.stop_reason === "end_turn") break;

    // ── EXECUTE TOOLS ──
    onPhase("action");
    const results = [];
    for (const tc of tu) {
      onLog("Cortex", `→ ${tc.name}`, T.cyan);
      const res = await exec(tc.name, tc.input);
      toolLog.push({ tool: tc.name, input: tc.input, iteration: iter, provider: route.provider });
      results.push({ type: "tool_result", tool_use_id: tc.id, content: res });
    }
    cur = [...cur, { role: "assistant", content: data.content }, { role: "user", content: results }];
  }

  return { text: texts.join("\n\n"), usage, toolLog, iterations: iter, routing };
}

// ─── STATE ─────────────────────────────────────────────────────────────────
const init = { msgs: [], log: [], mem: [], files: [], active: new Set(), status: "idle", phase: null, metrics: { tokens: 0, calls: 0, tools: 0, iters: 0 }, panels: { agents: true, sidebar: false }, autonomy: 2 };
function red(s, a) {
  switch (a.type) {
    case "MSG": return { ...s, msgs: [...s.msgs, a.p] };
    case "LOG": return { ...s, log: [...s.log.slice(-100), { ...a.p, ts: Date.now() }] };
    case "STATUS": return { ...s, status: a.p };
    case "PHASE": return { ...s, phase: a.p };
    case "ON": return { ...s, active: new Set([...s.active, a.p]) };
    case "OFF_ALL": return { ...s, active: new Set() };
    case "MET": return { ...s, metrics: { ...s.metrics, ...a.p } };
    case "PANEL": return { ...s, panels: { ...s.panels, [a.p]: !s.panels[a.p] } };
    case "AUTO": return { ...s, autonomy: a.p };
    case "MEM_STORE": return { ...s, mem: [...s.mem, a.payload].slice(-100) };
    case "FILE": return { ...s, files: [...s.files.filter(f => f.path !== a.payload.path), a.payload] };
    default: return s;
  }
}

// ─── UI PRIMITIVES (shadcn/ui patterns) ────────────────────────────────────

const Card = ({ children, className, style, ...props }) => (
  <div style={{ background: T.bg2, border: `1px solid ${T.border}`, borderRadius: "8px", ...style }} {...props}>{children}</div>
);
const CardHeader = ({ children, style }) => (
  <div style={{ padding: "12px 16px", borderBottom: `1px solid ${T.border}`, ...style }}>{children}</div>
);
const CardContent = ({ children, style }) => (
  <div style={{ padding: "12px 16px", ...style }}>{children}</div>
);
const Badge = ({ children, color = T.muted, bg, style }) => (
  <span style={{ display: "inline-flex", alignItems: "center", padding: "2px 8px", borderRadius: "9999px", fontSize: "11px", fontWeight: 600, fontFamily: font.mono, color, background: bg || `${color}18`, border: `1px solid ${color}30`, ...style }}>{children}</span>
);
const Separator = () => <div style={{ height: "1px", background: T.border, margin: "0" }} />;

// ─── PHASE INDICATOR ───────────────────────────────────────────────────────
function PhaseBar({ phase, status, iters }) {
  const p = ["thought", "plan", "action", "observation", "reflection"];
  const c = { thought: T.cyan, plan: T.amber, action: T.accent, observation: T.violet, reflection: T.rose };
  const idx = p.indexOf(phase);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px", padding: "6px 14px", background: T.bg2, borderRadius: "6px", border: `1px solid ${T.border}` }}>
      <span style={{ fontSize: "10px", color: T.muted, fontFamily: font.mono, letterSpacing: "1.5px", fontWeight: 600 }}>REACT</span>
      <div style={{ width: "1px", height: "14px", background: T.border }} />
      {p.map((name, i) => {
        const active = phase === name;
        const done = idx > i;
        return (
          <div key={name} style={{ display: "flex", alignItems: "center", gap: "4px" }}>
            <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: active ? c[name] : done ? `${c[name]}60` : T.bg3, border: `1.5px solid ${active ? c[name] : done ? `${c[name]}40` : T.border}`, boxShadow: active ? `0 0 8px ${c[name]}50` : "none", transition: "all 0.3s" }} />
            <span style={{ fontSize: "10px", fontFamily: font.mono, fontWeight: active ? 700 : 500, color: active ? c[name] : done ? T.muted : T.border2, textTransform: "uppercase", letterSpacing: "0.5px" }}>{name.slice(0, 4)}</span>
            {i < 4 && <span style={{ color: T.border2, fontSize: "10px", margin: "0 1px" }}>→</span>}
          </div>
        );
      })}
      {iters > 0 && <Badge color={T.accent} style={{ marginLeft: "4px" }}>×{iters}</Badge>}
      {status !== "idle" && <div style={{ marginLeft: "4px", width: "6px", height: "6px", borderRadius: "50%", background: T.accent, animation: "pulse 1.2s infinite" }} />}
    </div>
  );
}

// ─── AGENT CARD ────────────────────────────────────────────────────────────
function AgentCard({ agent, active }) {
  // Look up real status from agent registry if available
  const regEntry = _agentRegistry.find(a => a.name === agent.name?.toLowerCase() || a.metadata?.role === agent.role);
  const status = regEntry?.status || (active ? "online" : "unknown");
  const skillCount = regEntry?.skills?.length || 0;
  const statusColor = status === "online" ? "#22c55e" : status === "degraded" ? T.amber : status === "offline" ? T.rose : (active ? agent.color : T.bg3);

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "10px", padding: "10px 12px", borderRadius: "6px", background: active ? `${agent.color}12` : "transparent", border: `1px solid ${active ? `${agent.color}35` : "transparent"}`, transition: "all 0.25s" }}>
      <span style={{ fontSize: "18px", filter: active ? "none" : "grayscale(0.6) opacity(0.5)" }}>{agent.icon}</span>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: "13px", fontWeight: 600, color: active ? T.bright : T.muted, fontFamily: font.sans }}>{agent.name}</div>
        <div style={{ fontSize: "11px", color: active ? T.muted : T.border2 }}>{agent.role}{skillCount > 0 ? ` · ${skillCount} skills` : ""}</div>
      </div>
      <div title={status} style={{ width: "8px", height: "8px", borderRadius: "50%", background: statusColor, border: `1.5px solid ${statusColor}`, boxShadow: status === "online" ? `0 0 6px ${statusColor}40` : "none", transition: "all 0.3s" }} />
    </div>
  );
}

// ─── REGISTER AGENT BUTTON ─────────────────────────────────────────────────
function RegisterAgentButton({ log }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState("");

  const register = async () => {
    if (!url.trim()) return;
    setStatus("Fetching agent card...");
    try {
      // Try to fetch the agent's well-known card first
      let agentData = { name: new URL(url).hostname, url: url.trim(), description: "External agent" };
      try {
        const r = await fetch(`${url.trim().replace(/\/+$/, "")}/.well-known/agent.json`);
        if (r.ok) {
          const card = await r.json();
          agentData = { ...agentData, ...card };
        }
      } catch {}

      const r = await fetch(`${API_BASE}/api/agents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(agentData),
      });
      if (r.ok) {
        await fetchAgentRegistry();
        setStatus("Registered!");
        log("Cortex", `External agent registered: ${agentData.name}`, T.accent);
        setTimeout(() => { setOpen(false); setUrl(""); setStatus(""); }, 1000);
      } else {
        setStatus("Failed to register");
      }
    } catch (e) {
      setStatus(`Error: ${e.message}`);
    }
  };

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} style={{ marginTop: "8px", width: "100%", padding: "6px", fontSize: "11px", fontWeight: 600, fontFamily: font.mono, background: T.bg3, border: `1px solid ${T.border}`, color: T.muted, borderRadius: "5px", cursor: "pointer" }}>
        + Register Agent
      </button>
    );
  }

  return (
    <div style={{ marginTop: "8px", padding: "8px", background: T.bg3, borderRadius: "6px", border: `1px solid ${T.border}` }}>
      <div style={{ fontSize: "11px", fontWeight: 600, color: T.muted, marginBottom: "6px", fontFamily: font.mono }}>REGISTER EXTERNAL AGENT</div>
      <input value={url} onChange={e => setUrl(e.target.value)} placeholder="http://agent:port" style={{ width: "100%", padding: "6px 8px", fontSize: "12px", background: T.bg, border: `1px solid ${T.border}`, color: T.text, borderRadius: "4px", fontFamily: font.mono, outline: "none", boxSizing: "border-box" }} onKeyDown={e => e.key === "Enter" && register()} />
      <div style={{ display: "flex", gap: "4px", marginTop: "6px" }}>
        <button onClick={register} style={{ flex: 1, padding: "5px", fontSize: "11px", fontWeight: 600, background: `${T.accent}20`, border: `1px solid ${T.accent}40`, color: T.accent2, borderRadius: "4px", cursor: "pointer", fontFamily: font.mono }}>Register</button>
        <button onClick={() => { setOpen(false); setUrl(""); setStatus(""); }} style={{ padding: "5px 10px", fontSize: "11px", background: "transparent", border: `1px solid ${T.border}`, color: T.muted, borderRadius: "4px", cursor: "pointer", fontFamily: font.mono }}>×</button>
      </div>
      {status && <div style={{ fontSize: "10px", color: T.muted, marginTop: "4px", fontFamily: font.mono }}>{status}</div>}
    </div>
  );
}

// ─── MESSAGE BUBBLE ────────────────────────────────────────────────────────
function MsgBubble({ msg, msgIdx = 0, voiceAvailable = false, playTTS = null, ttsLoading = null }) {
  if (msg.role === "system") return (
    <div style={{ textAlign: "center", padding: "6px 16px", fontSize: "11px", color: T.muted, fontFamily: font.mono }}>{msg.content}</div>
  );
  const isUser = msg.role === "user";
  const phases = [];
  if (!isUser) {
    const rx = /\[(THOUGHT|PLAN|ACTION|RESULT|REFLECTION)\]([\s\S]*?)(?=\[(?:THOUGHT|PLAN|ACTION|RESULT|REFLECTION)\]|$)/gi;
    let m; while ((m = rx.exec(msg.content)) !== null) phases.push({ type: m[1].toLowerCase().replace("-", ""), text: m[2].trim() });
  }
  const pc = { thought: T.cyan, plan: T.amber, action: T.accent, result: T.violet, reflection: T.rose };
  const pi = { thought: "💭", plan: "📋", action: "⚡", result: "📦", reflection: "🔄" };
  const toolColors = { web_search: T.amber, code_exec: T.accent, file_write: T.accent, file_read: T.cyan, memory_store: T.violet, memory_query: T.violet, safety_check: T.orange, system_info: T.muted };

  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", padding: "4px 0" }}>
      <div style={{ maxWidth: isUser ? "72%" : "95%" }}>
        {isUser ? (
          <div style={{ padding: "12px 18px", borderRadius: "12px 12px 4px 12px", background: T.bg3, border: `1px solid ${T.border}`, fontSize: "14px", color: T.bright, lineHeight: 1.6 }}>{msg.content}</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
            {/* Tool call badges */}
            {msg.toolLog?.length > 0 && (
              <div style={{ display: "flex", gap: "5px", flexWrap: "wrap" }}>
                {msg.toolLog.map((l, i) => {
                  const short = l.tool.replace("ronin_", "");
                  return <Badge key={i} color={toolColors[short] || T.muted}>{short} <span style={{ opacity: 0.5, marginLeft: "3px" }}>iter:{l.iteration}</span></Badge>;
                })}
              </div>
            )}
            {/* Phase blocks or plain text */}
            {phases.length > 0 ? phases.map((p, i) => (
              <div key={i} style={{ padding: "10px 14px", borderRadius: "6px", background: `${pc[p.type]}08`, borderLeft: `3px solid ${pc[p.type]}60` }}>
                <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "4px" }}>
                  <span style={{ fontSize: "13px" }}>{pi[p.type]}</span>
                  <span style={{ fontSize: "11px", fontWeight: 700, color: pc[p.type], fontFamily: font.mono, textTransform: "uppercase", letterSpacing: "1px" }}>{p.type}</span>
                </div>
                <div style={{ fontSize: "13px", color: T.text, lineHeight: 1.65, whiteSpace: "pre-wrap" }}>{p.text}</div>
              </div>
            )) : (
              <div style={{ padding: "10px 14px", borderRadius: "6px", background: T.bg2, border: `1px solid ${T.border}` }}>
                <div style={{ fontSize: "13px", color: T.text, lineHeight: 1.65, whiteSpace: "pre-wrap" }}>{msg.content}</div>
              </div>
            )}
          </div>
        )}
        {!isUser && voiceAvailable && playTTS && (
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "2px" }}>
            <button
              onClick={() => playTTS(msg.content, msgIdx)}
              disabled={ttsLoading === msgIdx}
              title="Play with TTS"
              style={{ background: "transparent", border: "none", cursor: "pointer", fontSize: "14px", opacity: ttsLoading === msgIdx ? 0.5 : 0.4, padding: "2px 6px", borderRadius: "4px", transition: "opacity 0.2s" }}
              onMouseEnter={e => e.currentTarget.style.opacity = "0.9"}
              onMouseLeave={e => e.currentTarget.style.opacity = ttsLoading === msgIdx ? "0.5" : "0.4"}
            >
              {ttsLoading === msgIdx ? "⏳" : "🔊"}
            </button>
          </div>
        )}
        {msg.usage && (
          <div style={{ fontSize: "10px", color: T.border2, marginTop: "4px", fontFamily: font.mono, textAlign: "right" }}>
            {msg.usage.input_tokens?.toLocaleString()}↓ {msg.usage.output_tokens?.toLocaleString()}↑
            {msg.usage.cache_read > 0 ? ` · ${msg.usage.cache_read.toLocaleString()} cached` : ""}
            {msg.iterations > 1 ? ` · ${msg.iterations} iters` : ""}
            {msg.savings && (msg.savings.caching + msg.savings.toolFilter + msg.savings.compression + msg.savings.memory) > 0
              ? ` · ~${(msg.savings.caching + msg.savings.toolFilter + msg.savings.compression + msg.savings.memory).toLocaleString()} saved`
              : ""}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── MAIN ──────────────────────────────────────────────────────────────────
export default function Ronin() {
  const [s, d] = useReducer(red, init);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [iters, setIters] = useState(0);
  const chatRef = useRef(null);
  const inputRef = useRef(null);

  // Phase 4: Proactive intelligence state
  const [contextStream, setContextStream] = useState("");
  const [systemHealth, setSystemHealth] = useState({ disk_percent: 0, memory_percent: 0, cpu_percent: 0, event_queue_depth: 0 });
  const [schedules, setSchedules] = useState([]);
  const [eventFeed, setEventFeed] = useState([]);

  // Phase 6: Voice interface state
  const [voiceAvailable, setVoiceAvailable] = useState(false);
  const [voiceState, setVoiceState] = useState("idle"); // idle | recording | processing
  const [ttsLoading, setTtsLoading] = useState(null); // message index being loaded
  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const currentAudioRef = useRef(null);

  // Phase 6: Mobile tab state
  const [mobileTab, setMobileTab] = useState("chat"); // chat | memory | activity | agents

  useEffect(() => { chatRef.current?.scrollIntoView({ behavior: "smooth" }); }, [s.msgs]);

  // ── API Connection: check health, fetch tool schemas, load persisted memory ──
  const [apiStatus, setApiStatus] = useState("connecting"); // "connecting" | "connected" | "offline"
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const healthy = await checkApiHealth();
      if (cancelled) return;
      if (healthy) {
        setApiStatus("connected");
        log("Cortex", `API connected → ${API_BASE}`, T.accent);
        await fetchToolSchemas();
        await fetchAgentRegistry();
        await loadPersistedMemory(d);
        log("Cortex", `${MCP_TOOLS.length} tools loaded, ${_agentRegistry.length} agents registered, memory hydrated`, T.accent);
      } else {
        setApiStatus("offline");
        log("Cortex", `API offline — running in simulation mode. Start: python api.py`, T.amber);
      }
    })();
    // Periodic health check every 30s
    const interval = setInterval(async () => {
      const was = _apiConnected;
      const now = await checkApiHealth();
      if (!cancelled && now !== was) {
        setApiStatus(now ? "connected" : "offline");
        log("Cortex", now ? "API reconnected" : "API connection lost — simulation mode", now ? T.accent : T.amber);
        if (now && !was) { await fetchToolSchemas(); await fetchAgentRegistry(); await loadPersistedMemory(d); }
      }
    }, 30000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  // Phase 4: Periodic context stream + system health + schedules + events fetch
  useEffect(() => {
    if (apiStatus !== "connected") return;
    let cancelled = false;
    const fetchPhase4 = async () => {
      try {
        const [ctxR, healthR, schedR, evtR] = await Promise.allSettled([
          fetch(`${API_BASE}/api/context`).then(r => r.json()),
          fetch(`${API_BASE}/api/health`).then(r => r.json()),
          fetch(`${API_BASE}/api/schedules`).then(r => r.json()),
          fetch(`${API_BASE}/api/events?limit=20`).then(r => r.json()),
        ]);
        if (cancelled) return;
        if (ctxR.status === "fulfilled" && ctxR.value.has_content) setContextStream(ctxR.value.context);
        if (healthR.status === "fulfilled" && healthR.value.system) setSystemHealth(healthR.value.system);
        if (schedR.status === "fulfilled") setSchedules(schedR.value.schedules || []);
        if (evtR.status === "fulfilled") setEventFeed(evtR.value.events || []);
      } catch {}
    };
    fetchPhase4();
    const iv = setInterval(fetchPhase4, 30000);
    return () => { cancelled = true; clearInterval(iv); };
  }, [apiStatus]);

  // Phase 6: Voice availability check
  useEffect(() => {
    if (apiStatus !== "connected") return;
    fetch(`${API_BASE}/api/voice/status`)
      .then(r => r.json())
      .then(d => setVoiceAvailable(d.available === true))
      .catch(() => setVoiceAvailable(false));
  }, [apiStatus]);

  // Phase 6: Voice recording functions
  const startRecording = async () => {
    if (!voiceAvailable || voiceState !== "idle") return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunksRef.current = [];
      const mr = new MediaRecorder(stream);
      mr.ondataavailable = e => { if (e.data.size > 0) audioChunksRef.current.push(e.data); };
      mr.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        setVoiceState("processing");
        const blob = new Blob(audioChunksRef.current, { type: "audio/webm" });
        const form = new FormData();
        form.append("audio", blob, "recording.webm");
        try {
          const resp = await fetch(`${API_BASE}/api/voice/transcribe`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${localStorage.getItem("ronin_token") || ""}` },
            body: form,
          });
          if (resp.ok) {
            const data = await resp.json();
            setInput(prev => prev ? prev + " " + data.text : data.text);
          }
        } catch {}
        setVoiceState("idle");
      };
      mediaRecorderRef.current = mr;
      mr.start();
      setVoiceState("recording");
    } catch { setVoiceState("idle"); }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && voiceState === "recording") {
      mediaRecorderRef.current.stop();
    }
  };

  const playTTS = async (text, msgIdx) => {
    if (!voiceAvailable || ttsLoading === msgIdx) return;
    if (currentAudioRef.current) { currentAudioRef.current.pause(); currentAudioRef.current = null; }
    setTtsLoading(msgIdx);
    try {
      const resp = await fetch(`${API_BASE}/api/voice/synthesize`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${localStorage.getItem("ronin_token") || ""}`,
        },
        body: JSON.stringify({ text: text.slice(0, 1000) }),
      });
      if (resp.ok) {
        const audioBlob = await resp.blob();
        const url = URL.createObjectURL(audioBlob);
        const audio = new Audio(url);
        currentAudioRef.current = audio;
        audio.onended = () => { URL.revokeObjectURL(url); setTtsLoading(null); };
        audio.play();
      }
    } catch {}
    setTtsLoading(null);
  };

  const log = useCallback((agent, text, color) => d({ type: "LOG", p: { agent, text, color } }), []);
  const exec = useCallback(createExecutor(d, log), [log]);

  const send = useCallback(async (txt) => {
    if (!txt.trim() || busy) return;
    setBusy(true); setIters(0);
    d({ type: "MSG", p: { role: "user", content: txt } });
    d({ type: "STATUS", p: "thinking" });
    d({ type: "PHASE", p: "thought" });

    const lo = txt.toLowerCase();
    const ag = ["cortex", "guardian"];
    if (/search|find|look|research|what|who|when|news|latest/.test(lo)) ag.push("researcher");
    if (/code|build|create|fix|debug|deploy|script|program|function|api|app/.test(lo)) ag.push("engineer");
    if (/analy|data|trend|pattern|compar|stat|chart|pros|cons/.test(lo)) ag.push("analyst");
    if (/write|draft|email|doc|summar|explain|letter|proposal|blog/.test(lo)) ag.push("communicator");
    if (ag.length <= 2) ag.push("researcher", "communicator");
    ag.forEach(a => d({ type: "ON", p: a }));

    log("Cortex", "ReAct loop initialized", T.cyan);
    log("Aegis", "Safety bounds active", T.orange);

    // ── OPT 3: Conversation Compression — disabled ──
    const hist = s.msgs.slice(-12).filter(m => m.role === "user" || m.role === "assistant").map(m => ({ role: m.role, content: m.content }));
    hist.push({ role: "user", content: txt });

    // ── OPT 4: Memory Relevance — disabled ──
    let memCtx = "";

    const res = await agenticLoop(hist, SYS + memCtx + (contextStream ? "\n\n[CONTEXT]\n" + contextStream : ""), MCP_TOOLS, exec, p => d({ type: "PHASE", p }), log, 6, s.autonomy);
    setIters(res.iterations || 0);

    // Build routing info for display
    const routingInfo = res.routing ? ` | Route: Claude×${res.routing.claude || 0} Venice×${res.routing.venice || 0}` : "";
    const ttsiInfo = (res.ttsiResults || []).filter(t => !t.skipped).length;
    const ttsiSummary = ttsiInfo > 0 ? ` | TT-SI: ${ttsiInfo} check${ttsiInfo > 1 ? "s" : ""}` : "";
    const savedTotal = (res.savings?.caching || 0) + (res.savings?.toolFilter || 0) + (res.savings?.compression || 0) + (res.savings?.memory || 0);
    const savingsInfo = savedTotal > 0 ? ` | ~${savedTotal} tokens saved` : "";

    d({ type: "MSG", p: { role: "assistant", content: res.text || "Processing complete.", usage: res.usage, toolLog: res.toolLog, iterations: res.iterations, routing: res.routing, ttsiResults: res.ttsiResults, savings: res.savings } });
    d({ type: "MET", p: { tokens: s.metrics.tokens + (res.usage?.input_tokens || 0) + (res.usage?.output_tokens || 0), calls: s.metrics.calls + 1, tools: s.metrics.tools + (res.toolLog?.length || 0), iters: s.metrics.iters + (res.iterations || 0) } });

    d({ type: "PHASE", p: "reflection" });
    log("Cortex", `Done — ${res.iterations || 1} iters, ${res.toolLog?.length || 0} tools${routingInfo}${ttsiSummary}${savingsInfo}`, T.cyan);
    d({ type: "OFF_ALL" }); d({ type: "STATUS", p: "idle" }); d({ type: "PHASE", p: null });
    setBusy(false);
  }, [s.msgs, s.mem, s.metrics, s.autonomy, busy, exec, log]);

  const submit = () => { send(input); setInput(""); };
  const autoLabels = ["Manual", "Suggest", "Act+Confirm", "Autonomous"];

  return (
    <div style={{ width: "100%", height: "100vh", background: T.bg, color: T.text, fontFamily: font.sans, display: "flex", flexDirection: "column", overflow: "hidden" }}>

      {/* ═══ TOP BAR ═══ */}
      <div style={{ padding: "8px 16px", display: "flex", alignItems: "center", gap: "14px", borderBottom: `1px solid ${T.border}`, background: T.bg, zIndex: 10, flexShrink: 0 }}>
        {/* Logo */}
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <div style={{ width: "32px", height: "32px", borderRadius: "8px", background: `linear-gradient(135deg, ${T.accent}20, ${T.cyan}15)`, border: `1px solid ${T.accent}40`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "16px", fontWeight: 800, color: T.accent, fontFamily: font.mono }}>J</div>
          <div>
            <div style={{ fontSize: "15px", fontWeight: 700, color: T.bright, fontFamily: font.sans, letterSpacing: "0.5px" }}>RONIN</div>
            <div style={{ fontSize: "10px", color: T.muted, fontFamily: font.mono }}>v3.0 · shadcn · MCP</div>
          </div>
        </div>

        {/* API Status */}
        <div style={{ display: "flex", alignItems: "center", gap: "5px", padding: "3px 10px", borderRadius: "12px", background: apiStatus === "connected" ? `${T.accent}15` : apiStatus === "connecting" ? `${T.amber}15` : `${T.rose}15`, border: `1px solid ${apiStatus === "connected" ? T.accent : apiStatus === "connecting" ? T.amber : T.rose}30` }}>
          <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: apiStatus === "connected" ? T.accent : apiStatus === "connecting" ? T.amber : T.rose, animation: apiStatus === "connecting" ? "pulse 1.2s infinite" : "none" }} />
          <span style={{ fontSize: "10px", fontWeight: 600, color: apiStatus === "connected" ? T.accent : apiStatus === "connecting" ? T.amber : T.rose, fontFamily: font.mono }}>{apiStatus === "connected" ? "LIVE" : apiStatus === "connecting" ? "..." : "SIM"}</span>
        </div>

        <div style={{ width: "1px", height: "24px", background: T.border }} />
        <PhaseBar phase={s.phase} status={s.status} iters={iters} />

        <div style={{ marginLeft: "auto", display: "flex", gap: "16px", alignItems: "center" }}>
          {/* System Health (Phase 4) */}
          {apiStatus === "connected" && [
            { l: "Disk", v: `${systemHealth.disk_percent || 0}%`, c: systemHealth.disk_percent > 85 ? T.rose : T.muted },
            { l: "Mem", v: `${systemHealth.memory_percent || 0}%`, c: systemHealth.memory_percent > 80 ? T.rose : T.muted },
            { l: "Queue", v: systemHealth.event_queue_depth || 0, c: systemHealth.event_queue_depth > 100 ? T.amber : T.muted },
          ].map(m => (
            <div key={m.l} style={{ textAlign: "center" }}>
              <div style={{ fontSize: "12px", fontWeight: 600, color: m.c, fontFamily: font.mono }}>{m.v}</div>
              <div style={{ fontSize: "8px", color: T.border2, fontWeight: 500, letterSpacing: "0.5px" }}>{m.l}</div>
            </div>
          ))}
          {apiStatus === "connected" && <div style={{ width: "1px", height: "20px", background: T.border }} />}
          {/* Metrics */}
          {[
            { l: "Tokens", v: s.metrics.tokens.toLocaleString(), c: T.cyan },
            { l: "Tools", v: s.metrics.tools, c: T.accent },
            { l: "Iters", v: s.metrics.iters, c: T.amber },
            { l: "Tasks", v: s.metrics.calls, c: T.violet },
          ].map(m => (
            <div key={m.l} style={{ textAlign: "center" }}>
              <div style={{ fontSize: "14px", fontWeight: 700, color: m.c, fontFamily: font.mono }}>{m.v}</div>
              <div style={{ fontSize: "9px", color: T.muted, fontWeight: 500, letterSpacing: "0.5px" }}>{m.l}</div>
            </div>
          ))}
          <div style={{ width: "1px", height: "24px", background: T.border }} />
          {/* Panel toggles */}
          {[{ k: "agents", l: "Agents" }, { k: "sidebar", l: "Memory" }].map(b => (
            <button key={b.k} onClick={() => d({ type: "PANEL", p: b.k })} style={{ padding: "5px 12px", fontSize: "12px", fontWeight: 600, fontFamily: font.sans, background: s.panels[b.k] ? `${T.accent}18` : "transparent", border: `1px solid ${s.panels[b.k] ? T.accent + "40" : T.border}`, color: s.panels[b.k] ? T.accent2 : T.muted, borderRadius: "6px", cursor: "pointer", transition: "all 0.2s" }}>{b.l}</button>
          ))}
        </div>
      </div>

      {/* ═══ MAIN LAYOUT ═══ */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

        {/* LEFT: Agent Panel */}
        {s.panels.agents && (
          <div style={{ width: "220px", borderRight: `1px solid ${T.border}`, background: T.bg, overflowY: "auto", display: "flex", flexDirection: "column", flexShrink: 0, padding: "8px" }}>
            <div style={{ padding: "8px 12px 12px" }}>
              <div style={{ fontSize: "11px", fontWeight: 700, color: T.muted, fontFamily: font.mono, letterSpacing: "1.5px", marginBottom: "8px" }}>AGENT REGISTRY</div>
              <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
                {Object.values(AGENTS).map(a => <AgentCard key={a.id} agent={a} active={s.active.has(a.id)} />)}
                {/* External agents from registry */}
                {_agentRegistry.filter(a => !a.url?.startsWith("internal://")).map(a => (
                  <AgentCard key={a.name} agent={{ id: a.name, name: a.name, role: a.description?.slice(0, 30) || "External", icon: "🔗", color: T.muted }} active={false} />
                ))}
              </div>
              {/* Register Agent */}
              <RegisterAgentButton log={log} />
            </div>

            <Separator />

            {/* Autonomy */}
            <div style={{ padding: "12px" }}>
              <div style={{ fontSize: "11px", fontWeight: 700, color: T.muted, fontFamily: font.mono, letterSpacing: "1px", marginBottom: "8px" }}>AUTONOMY LEVEL</div>
              <div style={{ display: "flex", gap: "4px" }}>
                {[0, 1, 2, 3].map(i => (
                  <button key={i} onClick={() => d({ type: "AUTO", p: i })} style={{ flex: 1, padding: "6px 2px", fontSize: "10px", fontWeight: 600, fontFamily: font.mono, background: s.autonomy === i ? `${T.accent}20` : T.bg3, border: `1px solid ${s.autonomy === i ? T.accent + "50" : T.border}`, color: s.autonomy === i ? T.accent2 : T.muted, borderRadius: "5px", cursor: "pointer" }}>{i}</button>
                ))}
              </div>
              <div style={{ fontSize: "11px", color: T.muted, marginTop: "6px", textAlign: "center", fontWeight: 500 }}>{autoLabels[s.autonomy]}</div>
            </div>

            <Separator />

            {/* MCP Tools */}
            <div style={{ padding: "12px", overflowY: "auto" }}>
              <div style={{ fontSize: "11px", fontWeight: 700, color: T.muted, fontFamily: font.mono, letterSpacing: "1px", marginBottom: "8px" }}>MCP TOOLS</div>
              {MCP_TOOLS.map(t => {
                const short = t.name.replace("ronin_", "");
                const tColors = { web_search: T.amber, code_exec: T.accent, file_write: T.accent, file_read: T.cyan, memory_store: T.violet, memory_query: T.violet, safety_check: T.orange, system_info: T.muted };
                return (
                  <div key={t.name} style={{ display: "flex", alignItems: "center", gap: "8px", padding: "5px 4px", borderBottom: `1px solid ${T.border}20` }}>
                    <div style={{ width: "6px", height: "6px", borderRadius: "50%", background: tColors[short] || T.muted }} />
                    <span style={{ fontSize: "12px", color: T.text, fontFamily: font.mono, flex: 1 }}>{short}</span>
                  </div>
                );
              })}
            </div>

            {/* Phase 4: Scheduled Tasks */}
            {schedules.length > 0 && (
              <>
                <Separator />
                <div style={{ padding: "12px", overflowY: "auto" }}>
                  <div style={{ fontSize: "11px", fontWeight: 700, color: T.violet, fontFamily: font.mono, letterSpacing: "1px", marginBottom: "8px" }}>SCHEDULES ({schedules.length})</div>
                  {schedules.slice(0, 5).map(sch => (
                    <div key={sch.task_id} style={{ display: "flex", alignItems: "center", gap: "6px", padding: "4px 0", borderBottom: `1px solid ${T.border}20` }}>
                      <span style={{ fontSize: "10px", color: sch.enabled ? T.accent : T.border2 }}>{sch.enabled ? "⏰" : "⏸"}</span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: "11px", color: T.text, fontFamily: font.mono, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sch.name}</div>
                        <div style={{ fontSize: "9px", color: T.muted }}>{sch.cron_expression} · runs: {sch.run_count}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* CENTER: Chat */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          <div style={{ flex: 1, overflowY: "auto", padding: "16px 20px", scrollbarWidth: "thin" }}>
            {s.msgs.length === 0 && (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: "16px" }}>
                <div style={{ width: "56px", height: "56px", borderRadius: "14px", background: `linear-gradient(135deg, ${T.accent}15, ${T.cyan}10)`, border: `1px solid ${T.accent}25`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "28px", fontWeight: 800, color: `${T.accent}40`, fontFamily: font.mono }}>J</div>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: "18px", fontWeight: 700, color: T.text }}>RONIN {apiStatus === "connected" ? "Online" : apiStatus === "connecting" ? "Connecting..." : "Simulation Mode"}</div>
                  <div style={{ fontSize: "13px", color: T.muted, maxWidth: "420px", lineHeight: 1.5, marginTop: "6px" }}>
                    {apiStatus === "connected" ? `Real tool execution · ${MCP_TOOLS.length} tools · 6 agents · Persistent memory` : apiStatus === "connecting" ? "Connecting to RONIN API server..." : `JS simulation active · Start API: cd server && python api.py`}
                  </div>
                </div>
                <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", justifyContent: "center", maxWidth: "520px", marginTop: "8px" }}>
                  {[
                    "Build a Python REST API for task management",
                    "Research quantum computing breakthroughs 2026",
                    "Analyze solar vs wind energy for my business",
                    "Draft a proposal for AI consulting services",
                  ].map(s => (
                    <button key={s} onClick={() => setInput(s)} style={{ padding: "8px 14px", fontSize: "12px", fontWeight: 500, background: T.bg2, border: `1px solid ${T.border}`, color: T.text, borderRadius: "8px", cursor: "pointer", transition: "all 0.2s" }}>{s}</button>
                  ))}
                </div>
              </div>
            )}
            {s.msgs.map((m, i) => <MsgBubble key={i} msg={m} msgIdx={i} voiceAvailable={voiceAvailable} playTTS={playTTS} ttsLoading={ttsLoading} />)}
            <div ref={chatRef} />
          </div>

          {/* Input */}
          <div style={{ padding: "12px 20px", borderTop: `1px solid ${T.border}`, background: T.bg }}>
            <div style={{ display: "flex", gap: "8px", alignItems: "flex-end", background: T.bg2, borderRadius: "10px", border: `1px solid ${T.border}`, padding: "4px" }}>
              <textarea
                ref={inputRef}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
                placeholder={busy ? "Processing..." : "What do you need, Jay?"}
                disabled={busy}
                rows={1}
                style={{ flex: 1, background: "transparent", border: "none", outline: "none", color: T.bright, fontSize: "14px", padding: "10px 12px", resize: "none", fontFamily: font.sans, lineHeight: 1.4, minHeight: "20px", maxHeight: "120px" }}
              />
              {voiceAvailable && (
                <button
                  onMouseDown={startRecording}
                  onMouseUp={stopRecording}
                  onTouchStart={startRecording}
                  onTouchEnd={stopRecording}
                  disabled={busy || voiceState === "processing"}
                  title={voiceState === "recording" ? "Release to stop" : "Hold to record"}
                  style={{ padding: "10px 12px", borderRadius: "8px", background: voiceState === "recording" ? `${T.rose}30` : T.bg3, border: `1px solid ${voiceState === "recording" ? T.rose : T.border}`, color: voiceState === "recording" ? T.rose : T.muted, fontSize: "15px", cursor: "pointer", transition: "all 0.2s", flexShrink: 0, animation: voiceState === "recording" ? "pulse 1s infinite" : "none" }}
                >
                  {voiceState === "processing" ? "⏳" : "🎙️"}
                </button>
              )}
              <button onClick={submit} disabled={busy || !input.trim()} style={{ padding: "10px 22px", borderRadius: "8px", background: !busy && input.trim() ? T.accent : T.bg3, border: "none", color: !busy && input.trim() ? T.bg : T.border2, fontSize: "13px", fontWeight: 700, fontFamily: font.sans, cursor: busy ? "wait" : "pointer", transition: "all 0.2s", opacity: !busy && input.trim() ? 1 : 0.4 }}>
                {busy ? "Working..." : "Execute"}
              </button>
            </div>
          </div>
        </div>

        {/* RIGHT: Memory & Activity */}
        {s.panels.sidebar && (
          <div style={{ width: "260px", borderLeft: `1px solid ${T.border}`, background: T.bg, overflowY: "auto", display: "flex", flexDirection: "column", flexShrink: 0, padding: "8px" }}>
            {/* Semantic Memory */}
            <Card>
              <CardHeader><span style={{ fontSize: "11px", fontWeight: 700, color: T.accent2, fontFamily: font.mono, letterSpacing: "1px" }}>SEMANTIC MEMORY ({s.mem.length})</span></CardHeader>
              <CardContent>
                {s.mem.length === 0 ? <div style={{ fontSize: "12px", color: T.border2 }}>No facts stored yet</div> :
                  s.mem.slice(-6).map((m, i) => (
                    <div key={i} style={{ fontSize: "12px", color: T.text, padding: "4px 0", borderBottom: `1px solid ${T.border}20`, lineHeight: 1.4 }}>
                      <Badge color={T.accent} style={{ marginRight: "6px", fontSize: "9px" }}>{((m.confidence || .7) * 100).toFixed(0)}%</Badge>
                      {m.fact.slice(0, 80)}{m.fact.length > 80 ? "…" : ""}
                    </div>
                  ))
                }
              </CardContent>
            </Card>

            {/* Workspace Files */}
            <Card style={{ marginTop: "8px" }}>
              <CardHeader><span style={{ fontSize: "11px", fontWeight: 700, color: T.amber, fontFamily: font.mono, letterSpacing: "1px" }}>WORKSPACE ({s.files.length})</span></CardHeader>
              <CardContent>
                {s.files.length === 0 ? <div style={{ fontSize: "12px", color: T.border2 }}>No files yet</div> :
                  s.files.slice(-8).map((f, i) => (
                    <div key={i} style={{ fontSize: "12px", color: T.text, fontFamily: font.mono, padding: "3px 0" }}>
                      📄 {f.path} <span style={{ color: T.muted }}>({f.size}b)</span>
                    </div>
                  ))
                }
              </CardContent>
            </Card>

            {/* Activity Log */}
            <Card style={{ marginTop: "8px", flex: 1 }}>
              <CardHeader><span style={{ fontSize: "11px", fontWeight: 700, color: T.muted, fontFamily: font.mono, letterSpacing: "1px" }}>ACTIVITY LOG</span></CardHeader>
              <CardContent style={{ maxHeight: "250px", overflowY: "auto", padding: "8px 12px" }}>
                {/* Phase 4: Event Feed */}
                {eventFeed.length > 0 && eventFeed.slice(0, 8).map((ev, i) => {
                  const srcIcon = { filesystem: "📁", webhook: "🔔", schedule: "⏰", system: "💻", manual: "👤" }[ev.source] || "•";
                  const srcColor = { filesystem: T.cyan, webhook: T.amber, schedule: T.violet, system: T.orange, manual: T.muted }[ev.source] || T.muted;
                  return (
                    <div key={ev.event_id || i} style={{ fontSize: "11px", color: T.muted, padding: "2px 0", lineHeight: 1.4 }}>
                      <span style={{ color: T.border2, fontFamily: font.mono, fontSize: "10px" }}>{(ev.created_at || "").slice(11, 19)}</span>{" "}
                      <span title={ev.source}>{srcIcon}</span>{" "}
                      <span style={{ color: srcColor, fontWeight: 600 }}>{ev.event_type.replace("webhook_", "").replace("file_", "").replace("system_", "").replace("cron_", "")}</span>
                      {ev.error && <span style={{ color: T.rose, fontSize: "10px" }}> ✗</span>}
                    </div>
                  );
                })}
                {eventFeed.length > 0 && s.log.length > 0 && <div style={{ height: "1px", background: T.border, margin: "4px 0" }} />}
                {/* Original activity log entries */}
                {s.log.length === 0 && eventFeed.length === 0 ? <div style={{ fontSize: "12px", color: T.border2, textAlign: "center", padding: "12px" }}>Awaiting directives...</div> :
                  s.log.slice(-30).map((l, i) => (
                    <div key={i} style={{ fontSize: "11px", color: T.muted, padding: "2px 0", lineHeight: 1.4 }}>
                      <span style={{ color: T.border2, fontFamily: font.mono, fontSize: "10px" }}>{new Date(l.ts).toLocaleTimeString()}</span>{" "}
                      <span style={{ color: l.color || T.cyan, fontWeight: 600 }}>{l.agent}</span>{" "}
                      <span style={{ color: T.text }}>{l.text}</span>
                    </div>
                  ))
                }
              </CardContent>
            </Card>
          </div>
        )}
      </div>

      {/* Phase 6: Mobile bottom tab bar */}
      <div style={{ display: "none" }} className="ronin-mobile-tabs">
        <div style={{ position: "fixed", bottom: 0, left: 0, right: 0, background: T.bg, borderTop: `1px solid ${T.border}`, display: "flex", zIndex: 100, height: "52px" }}>
          {[
            { id: "chat", icon: "💬", label: "Chat" },
            { id: "memory", icon: "🧠", label: "Memory" },
            { id: "activity", icon: "📋", label: "Activity" },
            { id: "agents", icon: "🤖", label: "Agents" },
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setMobileTab(tab.id)}
              style={{ flex: 1, background: "transparent", border: "none", color: mobileTab === tab.id ? T.accent : T.muted, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "2px", cursor: "pointer", fontSize: "11px", fontFamily: font.sans, fontWeight: mobileTab === tab.id ? 700 : 500, padding: "4px 0" }}
            >
              <span style={{ fontSize: "18px" }}>{tab.icon}</span>
              <span>{tab.label}</span>
            </button>
          ))}
        </div>
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap');
        @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.15 } }
        @media (max-width: 768px) {
          .ronin-mobile-tabs { display: block !important; }
          .ronin-sidebar { display: none !important; }
          .ronin-agents-panel { display: none !important; }
          .ronin-chat-area { padding-bottom: 60px !important; }
          .ronin-msg-bubble { max-width: 95vw !important; }
          .ronin-phase-bar { overflow-x: auto; }
          .ronin-metrics { display: none !important; }
          .ronin-cost-badge { display: inline-flex !important; }
        }
        ::-webkit-scrollbar { width:6px }
        ::-webkit-scrollbar-track { background:${T.bg} }
        ::-webkit-scrollbar-thumb { background:${T.border};border-radius:3px }
        ::-webkit-scrollbar-thumb:hover { background:${T.border2} }
        * { box-sizing:border-box; margin:0; padding:0 }
        textarea::placeholder { color:${T.border2} }
        button { transition:all 0.15s }
        button:hover:not(:disabled) { filter:brightness(1.1) }
        button:active:not(:disabled) { transform:scale(0.98) }
      `}</style>
    </div>
  );
}
