"""
RONIN Slack Bot Integration
============================
Handles incoming Slack events (app_mention, DM messages) and slash commands.
Sends formatted responses back via the Slack Web API using httpx.

No Slack SDK — uses httpx directly (already a project dependency).

Setup:
    SLACK_BOT_TOKEN  → xoxb-... token stored in Vault
    SLACK_SIGNING_SECRET → signing secret for webhook verification
"""

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import httpx

SLACK_API_BASE = "https://slack.com/api"


# ─── Signature Verification ────────────────────────────────────────────────

def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
) -> bool:
    """
    Verify a Slack request signature using HMAC-SHA256.
    Rejects requests older than 5 minutes (replay protection).
    """
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False

    # Replay protection: reject requests older than 5 minutes
    if abs(time.time() - ts) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature)


# ─── Message Sender ────────────────────────────────────────────────────────

async def send_slack_message(
    channel: str,
    text: str,
    http: httpx.AsyncClient,
    bot_token: str,
    thread_ts: Optional[str] = None,
) -> bool:
    """
    POST a message to a Slack channel via chat.postMessage.
    Formats the response as Slack blocks for better readability.
    Returns True on success, False on failure.
    """
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text[:3000],  # Slack block text limit
            },
        }
    ]

    payload: Dict[str, Any] = {
        "channel": channel,
        "text": text[:150],  # Fallback text for notifications
        "blocks": blocks,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    try:
        resp = await http.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload).encode(),
        )
        data = resp.json()
        return bool(data.get("ok"))
    except Exception:
        return False


# ─── Event Normalization ───────────────────────────────────────────────────

def normalize_slack_event(raw: Dict) -> Dict:
    """
    Normalize a raw Slack event payload to a consistent dict.
    Handles app_mention, message, and slash command types.
    """
    event = raw.get("event", raw)
    event_type = event.get("type", "unknown")

    return {
        "type": event_type,
        "channel": event.get("channel", ""),
        "text": _clean_text(event.get("text", "")),
        "user": event.get("user", ""),
        "ts": event.get("ts", ""),
        "thread_ts": event.get("thread_ts"),
        "is_bot": _is_bot_message(event),
        "team_id": raw.get("team_id", ""),
        "event_id": raw.get("event_id", ""),
    }


def _clean_text(text: str) -> str:
    """Strip Slack mention syntax like <@U12345> from message text."""
    import re
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def _is_bot_message(event: Dict) -> bool:
    """Return True if the event was sent by a bot (to prevent loops)."""
    return bool(
        event.get("bot_id")
        or event.get("subtype") == "bot_message"
        or event.get("bot_profile")
    )


# ─── Event Handler ─────────────────────────────────────────────────────────

async def handle_slack_event(
    event_payload: Dict,
    http: httpx.AsyncClient,
    bot_token: str,
    api_base_url: str,
    auth_token: str,
) -> None:
    """
    Process a normalized Slack event.
    Ignores bot messages. Forwards user messages to /api/cli/run and replies.
    """
    normalized = normalize_slack_event(event_payload)

    if normalized["is_bot"]:
        return  # Prevent bot loops

    event_type = normalized["type"]
    if event_type not in ("app_mention", "message"):
        return

    command = normalized["text"]
    if not command:
        return

    channel = normalized["channel"]
    thread_ts = normalized.get("thread_ts") or normalized.get("ts")

    # Run through RONIN CLI endpoint
    try:
        resp = await http.post(
            f"{api_base_url}/api/cli/run",
            json={"command": command, "autonomy": 0.5},
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=30.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            result_text = data.get("result", "No response.")
            tier = data.get("tier", "unknown")
            reply = f"*RONIN* (tier: `{tier}`)\n{result_text}"
        else:
            reply = f"⚠️ RONIN returned error {resp.status_code}."
    except Exception as e:
        reply = f"⚠️ RONIN unavailable: {e}"

    await send_slack_message(channel, reply, http, bot_token, thread_ts=thread_ts)


# ─── Slash Command Handler ─────────────────────────────────────────────────

async def dispatch_slash_command(
    command: str,
    text: str,
    response_url: str,
    http: httpx.AsyncClient,
    bot_token: str,
    api_base_url: str,
    auth_token: str,
) -> None:
    """
    Handle a Slack slash command by routing to RONIN and posting
    a deferred response to response_url.
    """
    text = text.strip().lower()

    if text == "status":
        try:
            resp = await http.get(f"{api_base_url}/api/health")
            health = resp.json() if resp.status_code == 200 else {}
            result = build_slack_status_response(health)
        except Exception as e:
            result = f"⚠️ Could not fetch status: {e}"

    elif text == "memory":
        try:
            resp = await http.get(
                f"{api_base_url}/api/memory/semantic",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            if resp.status_code == 200:
                memories = resp.json().get("memories", [])[:5]
                lines = [f"• {m.get('fact', '')}" for m in memories]
                result = "*Top Memories:*\n" + "\n".join(lines) if lines else "No memories stored."
            else:
                result = "Could not retrieve memories."
        except Exception as e:
            result = f"⚠️ Error: {e}"

    elif text == "schedule list" or text == "schedules":
        try:
            resp = await http.get(
                f"{api_base_url}/api/schedules",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            if resp.status_code == 200:
                schedules = resp.json().get("schedules", [])[:5]
                lines = [
                    f"• `{s.get('name')}` — `{s.get('cron_expression')}` "
                    f"({'enabled' if s.get('enabled') else 'disabled'})"
                    for s in schedules
                ]
                result = "*Active Schedules:*\n" + "\n".join(lines) if lines else "No schedules configured."
            else:
                result = "Could not retrieve schedules."
        except Exception as e:
            result = f"⚠️ Error: {e}"

    else:
        # Default: run as RONIN command
        try:
            resp = await http.post(
                f"{api_base_url}/api/cli/run",
                json={"command": text or command, "autonomy": 0.5},
                headers={"Authorization": f"Bearer {auth_token}"},
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result", "No response.")
            else:
                result = f"RONIN returned {resp.status_code}."
        except Exception as e:
            result = f"⚠️ RONIN unavailable: {e}"

    # Post deferred response
    try:
        await http.post(
            response_url,
            json={"response_type": "in_channel", "text": result},
        )
    except Exception:
        pass


# ─── Status Formatter ──────────────────────────────────────────────────────

def build_slack_status_response(health: Dict) -> str:
    """Format a health response dict as a Slack-friendly status string."""
    status = health.get("status", "unknown")
    uptime = health.get("uptime_seconds", 0)
    db = health.get("database", {})
    system = health.get("system", {})

    lines = [
        f"*RONIN Status:* `{status}`",
        f"Uptime: {int(uptime // 3600)}h {int((uptime % 3600) // 60)}m",
    ]

    if db:
        lines.append(
            f"Memory: {db.get('semantic_memories', 0)} semantic, "
            f"{db.get('episodic_memories', 0)} episodic"
        )

    if system:
        lines.append(
            f"System: CPU {system.get('cpu_percent', '?')}% | "
            f"Mem {system.get('memory_percent', '?')}% | "
            f"Disk {system.get('disk_percent', '?')}%"
        )

    return "\n".join(lines)
