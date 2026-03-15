"""
RONIN Notification System — Multi-Channel Alert Routing
=========================================================
P2 deliverable for Phase 4.

Routes notifications to configured channels: log, webhook_out,
slack, email. Channel config stored in KV store.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from event_queue import Event, EventBus, EventPriority

logger = logging.getLogger("RoninNotifications")


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════

class NotificationChannel(str, Enum):
    log = "log"
    webhook_out = "webhook_out"
    slack = "slack"
    email = "email"


class Notification(BaseModel):
    channel: NotificationChannel
    recipient: str = ""  # URL, email, or empty for log
    title: str
    body: str
    priority: EventPriority = EventPriority.normal
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChannelConfig(BaseModel):
    """Configuration for a notification channel."""
    enabled: bool = False
    min_priority: EventPriority = EventPriority.normal
    recipient: str = ""  # channel-specific: URL, email, webhook URL
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NotificationConfig(BaseModel):
    """Full notification configuration."""
    log: ChannelConfig = Field(default_factory=lambda: ChannelConfig(enabled=True, min_priority=EventPriority.low))
    webhook_out: ChannelConfig = Field(default_factory=ChannelConfig)
    slack: ChannelConfig = Field(default_factory=ChannelConfig)
    email: ChannelConfig = Field(default_factory=ChannelConfig)


# ═══════════════════════════════════════════════════════════════════════════
# NOTIFICATION ROUTER
# ═══════════════════════════════════════════════════════════════════════════

PRIORITY_RANK = {"low": 3, "normal": 2, "high": 1, "critical": 0}


class NotificationRouter:
    """Routes notifications to configured channels."""

    def __init__(self, db: sqlite3.Connection, http: Optional[httpx.AsyncClient] = None):
        self.db = db
        self.http = http
        self._config: Optional[NotificationConfig] = None

    def get_config(self) -> NotificationConfig:
        """Load notification config from KV store."""
        if self._config:
            return self._config
        try:
            row = self.db.execute(
                "SELECT value FROM key_value_store WHERE key = ?",
                ("config:notifications",),
            ).fetchone()
            if row:
                data = json.loads(row["value"])
                self._config = NotificationConfig(**data)
                return self._config
        except Exception:
            pass
        self._config = NotificationConfig()
        return self._config

    def save_config(self, config: NotificationConfig) -> None:
        """Save notification config to KV store."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT OR REPLACE INTO key_value_store (key, value, updated_at)
               VALUES (?, ?, ?)""",
            ("config:notifications", config.model_dump_json(), now),
        )
        self.db.commit()
        self._config = config

    async def send(self, notification: Notification) -> Dict[str, Any]:
        """Route a notification to the appropriate channel."""
        config = self.get_config()
        results: Dict[str, Any] = {}

        # Always try the specified channel
        channel_config = getattr(config, notification.channel.value, None)
        if channel_config and channel_config.enabled:
            if self._passes_priority(notification.priority, channel_config.min_priority):
                result = await self._send_to_channel(notification, channel_config)
                results[notification.channel.value] = result

        return results

    async def send_to_all(self, title: str, body: str, priority: EventPriority = EventPriority.normal) -> Dict[str, Any]:
        """Send notification to all enabled channels that meet priority threshold."""
        config = self.get_config()
        results: Dict[str, Any] = {}

        for channel_name in ["log", "webhook_out", "slack", "email"]:
            channel_config = getattr(config, channel_name, None)
            if not channel_config or not channel_config.enabled:
                continue
            if not self._passes_priority(priority, channel_config.min_priority):
                continue

            notif = Notification(
                channel=NotificationChannel(channel_name),
                recipient=channel_config.recipient,
                title=title,
                body=body,
                priority=priority,
            )
            result = await self._send_to_channel(notif, channel_config)
            results[channel_name] = result

        return results

    async def send_test(self) -> Dict[str, Any]:
        """Send a test notification to all enabled channels."""
        return await self.send_to_all(
            title="RONIN Test Notification",
            body="This is a test notification from RONIN. If you see this, the channel is working.",
            priority=EventPriority.normal,
        )

    # ─── Channel Implementations ──────────────────────────────────────────

    async def _send_to_channel(self, notification: Notification, config: ChannelConfig) -> Dict[str, Any]:
        """Dispatch to the appropriate channel implementation."""
        try:
            if notification.channel == NotificationChannel.log:
                return self._send_log(notification)
            elif notification.channel == NotificationChannel.webhook_out:
                return await self._send_webhook(notification, config)
            elif notification.channel == NotificationChannel.slack:
                return await self._send_slack(notification, config)
            elif notification.channel == NotificationChannel.email:
                return self._send_email_stub(notification, config)
            else:
                return {"sent": False, "error": f"Unknown channel: {notification.channel}"}
        except Exception as e:
            return {"sent": False, "error": str(e)}

    def _send_log(self, notification: Notification) -> Dict[str, Any]:
        """Write to audit log / activity log."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO audit_log (timestamp, tool_name, agent, input_summary, output_summary, success, execution_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, "notification", "system", notification.title, notification.body[:500], 1, 0.0),
        )
        self.db.commit()
        logger.info(f"[NOTIFICATION] {notification.title}: {notification.body[:100]}")
        return {"sent": True, "channel": "log"}

    async def _send_webhook(self, notification: Notification, config: ChannelConfig) -> Dict[str, Any]:
        """POST notification to a webhook URL."""
        url = notification.recipient or config.recipient
        if not url:
            return {"sent": False, "error": "No webhook URL configured"}
        if not self.http:
            return {"sent": False, "error": "HTTP client not available"}

        payload = {
            "title": notification.title,
            "body": notification.body,
            "priority": notification.priority.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": notification.metadata,
        }
        try:
            resp = await self.http.post(url, json=payload, timeout=10.0)
            return {"sent": resp.status_code < 400, "status_code": resp.status_code}
        except Exception as e:
            return {"sent": False, "error": str(e)}

    async def _send_slack(self, notification: Notification, config: ChannelConfig) -> Dict[str, Any]:
        """POST to Slack incoming webhook."""
        url = notification.recipient or config.recipient
        if not url:
            return {"sent": False, "error": "No Slack webhook URL configured"}
        if not self.http:
            return {"sent": False, "error": "HTTP client not available"}

        emoji = {"critical": "🚨", "high": "⚠️", "normal": "ℹ️", "low": "📝"}.get(notification.priority.value, "")
        payload = {
            "text": f"{emoji} *{notification.title}*\n{notification.body}",
        }
        try:
            resp = await self.http.post(url, json=payload, timeout=10.0)
            return {"sent": resp.status_code < 400, "status_code": resp.status_code}
        except Exception as e:
            return {"sent": False, "error": str(e)}

    def _send_email_stub(self, notification: Notification, config: ChannelConfig) -> Dict[str, Any]:
        """Email sending stub — requires SMTP config in .env."""
        logger.info(f"Email notification (stub): {notification.title}")
        return {"sent": False, "error": "Email not configured (SMTP settings required in .env)"}

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _passes_priority(self, event_priority: EventPriority, min_priority: EventPriority) -> bool:
        """Check if event priority meets minimum threshold."""
        return PRIORITY_RANK.get(event_priority.value, 2) <= PRIORITY_RANK.get(min_priority.value, 2)


# ═══════════════════════════════════════════════════════════════════════════
# EVENT BUS INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════

def create_notification_handler(router: NotificationRouter):
    """
    Create an EventBus handler that sends notifications for
    high and critical priority events.
    """
    async def _handler(event: Event) -> Optional[str]:
        if event.priority in (EventPriority.high, EventPriority.critical):
            title = f"RONIN Alert: {event.event_type}"
            body = f"Source: {event.source.value}\nType: {event.event_type}\nPayload: {json.dumps(event.payload, indent=2)[:500]}"
            results = await router.send_to_all(title, body, event.priority)
            return json.dumps(results)
        return None

    return _handler
