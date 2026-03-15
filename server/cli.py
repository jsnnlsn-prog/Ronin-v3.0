#!/usr/bin/env python3
"""
RONIN CLI Client
=================
Standalone terminal interface to RONIN API.
Requires only: httpx, rich

Usage:
    python cli.py                          # Interactive REPL
    python cli.py "list my schedules"      # Single command
    python cli.py --watch                  # Stream live events
    python cli.py --status                 # Print health and exit

Config:
    RONIN_API env var sets base URL (default: http://localhost:8742)
    Token stored at ~/.ronin/cli_token
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    print("httpx required: pip install httpx")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH = True
except ImportError:
    RICH = False

# ─── Config ───────────────────────────────────────────────────────────────

API_BASE = os.environ.get("RONIN_API", "http://localhost:8742")
TOKEN_PATH = Path.home() / ".ronin" / "cli_token"
console = Console() if RICH else None


# ─── Token Management ─────────────────────────────────────────────────────

def load_token() -> Optional[str]:
    """Load stored auth token, return None if not found."""
    try:
        return TOKEN_PATH.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


def save_token(token: str) -> None:
    """Save auth token to ~/.ronin/cli_token."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)


def auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─── Formatters ───────────────────────────────────────────────────────────

def format_status(health: Dict) -> str:
    """Format health dict as a readable string."""
    status = health.get("status", "unknown")
    uptime = health.get("uptime_seconds", 0)
    db = health.get("database", {})
    system = health.get("system", {})

    lines = [
        f"Status:  {status}",
        f"Uptime:  {int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s",
    ]
    if db:
        lines += [
            f"Memory:  {db.get('semantic_memories', 0)} semantic, {db.get('episodic_memories', 0)} episodic",
            f"Events:  {db.get('events', 0)} total",
        ]
    if system:
        lines += [
            f"CPU:     {system.get('cpu_percent', '?')}%",
            f"Memory:  {system.get('memory_percent', '?')}%",
            f"Disk:    {system.get('disk_percent', '?')}%",
        ]
    return "\n".join(lines)


def format_memories(memories: List[Dict]) -> str:
    """Format memory list."""
    if not memories:
        return "(no memories)"
    lines = []
    for m in memories:
        conf = int(m.get("confidence", 0) * 100)
        lines.append(f"  [{conf:3d}%] {m.get('fact', '')}")
    return "\n".join(lines)


def format_schedules(schedules: List[Dict]) -> str:
    """Format schedule list."""
    if not schedules:
        return "(no schedules)"
    lines = []
    for s in schedules:
        enabled = "✓" if s.get("enabled") else "✗"
        lines.append(
            f"  [{enabled}] {s.get('name', '?'):<30} {s.get('cron_expression', '?')} "
            f"(runs: {s.get('run_count', 0)})"
        )
    return "\n".join(lines)


def format_event(event: Dict) -> str:
    """Format a single event for terminal display."""
    src = event.get("source", "?")
    etype = event.get("event_type", "?")
    ts = event.get("created_at", "")[:19]
    processed = "✓" if event.get("processed") else "⏳"
    priority = event.get("priority", "normal")
    return f"{processed} [{ts}] [{src}/{priority}] {etype}"


def print_result(result: str, tier: str = "", cost: float = 0.0) -> None:
    """Print a RONIN result, using Rich if available."""
    if RICH and console:
        subtitle = f"tier: {tier}" if tier else ""
        if cost:
            subtitle += f" | ${cost:.4f}"
        console.print(Panel(result, title="[bold cyan]RONIN[/bold cyan]", subtitle=subtitle))
    else:
        if tier:
            print(f"[RONIN:{tier}]")
        print(result)
        if cost:
            print(f"[cost: ${cost:.4f}]")


# ─── API Calls ─────────────────────────────────────────────────────────────

def run_command(command: str, token: str, autonomy: float = 0.5) -> Dict:
    """POST /api/cli/run and return the response dict."""
    with httpx.Client(base_url=API_BASE, timeout=60.0) as client:
        resp = client.post(
            "/api/cli/run",
            json={"command": command, "autonomy": autonomy},
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


def get_health() -> Dict:
    """GET /api/health."""
    with httpx.Client(base_url=API_BASE, timeout=10.0) as client:
        resp = client.get("/api/health")
        resp.raise_for_status()
        return resp.json()


def get_events(since_ts: Optional[str] = None) -> List[Dict]:
    """GET /api/events with optional since filter."""
    params = {"processed": "false", "limit": 20}
    if since_ts:
        params["after"] = since_ts
    with httpx.Client(base_url=API_BASE, timeout=10.0) as client:
        resp = client.get("/api/events", params=params)
        if resp.status_code == 200:
            return resp.json().get("events", [])
        return []


# ─── Modes ────────────────────────────────────────────────────────────────

def handle_shortcut(cmd: str, token: str) -> bool:
    """Handle !shortcuts. Returns True if handled, False otherwise."""
    lower = cmd.strip().lower()

    if lower == "!memory":
        with httpx.Client(base_url=API_BASE, timeout=10.0) as client:
            resp = client.get("/api/memory/semantic", headers=auth_headers(token))
            if resp.status_code == 200:
                mems = resp.json().get("memories", [])
                print(format_memories(mems))
            else:
                print(f"Error {resp.status_code}")
        return True

    if lower == "!status":
        try:
            print(format_status(get_health()))
        except Exception as e:
            print(f"Error: {e}")
        return True

    if lower in ("!schedule list", "!schedules"):
        with httpx.Client(base_url=API_BASE, timeout=10.0) as client:
            resp = client.get("/api/schedules", headers=auth_headers(token))
            if resp.status_code == 200:
                scheds = resp.json().get("schedules", [])
                print(format_schedules(scheds))
            else:
                print(f"Error {resp.status_code}")
        return True

    return False


def interactive_mode(token: str) -> None:
    """Interactive REPL loop."""
    if RICH and console:
        console.print("[bold cyan]RONIN CLI[/bold cyan] — type [italic]exit[/italic] to quit")
    else:
        print("RONIN CLI — type 'exit' to quit")

    while True:
        try:
            cmd = input("ronin> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd:
            continue
        if cmd.lower() in ("exit", "quit", "q"):
            break
        if handle_shortcut(cmd, token):
            continue

        try:
            data = run_command(cmd, token)
            print_result(
                data.get("result", ""),
                tier=data.get("tier", ""),
                cost=data.get("cost_usd", 0.0),
            )
        except httpx.HTTPStatusError as e:
            print(f"Error {e.response.status_code}: {e.response.text}")
        except Exception as e:
            print(f"Error: {e}")


def single_command_mode(command: str, token: str) -> None:
    """Run one command and exit."""
    try:
        data = run_command(command, token)
        print_result(
            data.get("result", ""),
            tier=data.get("tier", ""),
            cost=data.get("cost_usd", 0.0),
        )
    except httpx.HTTPStatusError as e:
        print(f"Error {e.response.status_code}: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def watch_mode(token: str) -> None:
    """Poll /api/events every 5 seconds and print new events."""
    if RICH and console:
        console.print("[bold cyan]RONIN Watch Mode[/bold cyan] — Ctrl+C to exit")
    else:
        print("RONIN Watch Mode — Ctrl+C to exit")

    seen: set = set()
    try:
        while True:
            try:
                events = get_events()
                for evt in events:
                    eid = evt.get("event_id", "")
                    if eid and eid not in seen:
                        seen.add(eid)
                        line = format_event(evt)
                        if RICH and console:
                            src = evt.get("source", "?")
                            colors = {
                                "filesystem": "cyan",
                                "webhook": "yellow",
                                "schedule": "magenta",
                                "system": "red",
                                "manual": "green",
                            }
                            color = colors.get(src, "white")
                            console.print(f"[{color}]{line}[/{color}]")
                        else:
                            print(line)
            except Exception as e:
                print(f"Watch error: {e}")
            time.sleep(5)
    except KeyboardInterrupt:
        print()


def status_mode() -> None:
    """Print health and exit."""
    try:
        health = get_health()
        output = format_status(health)
        if RICH and console:
            console.print(Panel(output, title="[bold cyan]RONIN Status[/bold cyan]"))
        else:
            print(output)
    except Exception as e:
        print(f"Could not connect to RONIN at {API_BASE}: {e}")
        sys.exit(1)


# ─── Entry Point ───────────────────────────────────────────────────────────

def _update_api_base(new_base: str) -> None:
    """Update the module-level API_BASE."""
    global API_BASE
    API_BASE = new_base



def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="RONIN CLI — terminal interface to RONIN agent system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli.py                          # Interactive REPL
  python cli.py "list my schedules"      # Single command
  python cli.py --watch                  # Stream live events
  python cli.py --status                 # Print health and exit

Shortcuts (in REPL):
  !memory          List semantic memories
  !status          Print system status
  !schedule list   List active schedules
        """,
    )
    parser.add_argument("command", nargs="?", help="Single command to run then exit")
    parser.add_argument("--watch", action="store_true", help="Stream live events")
    parser.add_argument("--status", action="store_true", help="Print health and exit")
    parser.add_argument(
        "--api",
        default=API_BASE,
        help=f"RONIN API base URL (default: {API_BASE})",
    )
    args = parser.parse_args()

    # Override API base if provided
    if args.api != API_BASE:
        _update_api_base(args.api)

    if args.status:
        status_mode()
        return

    if args.watch:
        token = load_token() or "anonymous"
        watch_mode(token)
        return

    token = load_token()
    if not token:
        # Try to get a token (anonymous mode for status, needs auth for commands)
        print(
            f"No auth token found at {TOKEN_PATH}\n"
            "Set one with: echo 'YOUR_TOKEN' > ~/.ronin/cli_token\n"
            "Or use --status for unauthenticated health check."
        )
        if not args.command:
            # Still allow REPL, errors will be 401
            token = ""
        else:
            sys.exit(1)

    if args.command:
        single_command_mode(args.command, token)
    else:
        interactive_mode(token)


if __name__ == "__main__":
    main()
