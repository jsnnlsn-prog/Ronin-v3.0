"""
Tests for Phase 6 Voice endpoints: /api/voice/status, /api/voice/transcribe, /api/voice/synthesize
"""

import io
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api
from api import app
from auth import require_auth, User
from resilience import set_test_mode

set_test_mode(True)


# ─── Auth bypass ──────────────────────────────────────────────────────────

def _dummy_user():
    u = MagicMock(spec=User)
    u.username = "testuser"
    u.is_admin = False
    return u


@pytest.fixture(autouse=True)
def override_auth():
    app.dependency_overrides[require_auth] = lambda: _dummy_user()
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── Tests ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_status_no_key(client, monkeypatch):
    """Returns available=false when no OpenAI key is configured."""
    monkeypatch.setattr(api, "_get_openai_key", lambda: None)
    resp = await client.get("/api/voice/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert "OPENAI_API_KEY" in data["reason"]


@pytest.mark.asyncio
async def test_voice_status_with_key(client, monkeypatch):
    """Returns available=true when OpenAI key is present."""
    monkeypatch.setattr(api, "_get_openai_key", lambda: "sk-test-key")
    resp = await client.get("/api/voice/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True


@pytest.mark.asyncio
async def test_transcribe_no_key(client, monkeypatch):
    """Returns 503 when no OpenAI key configured."""
    monkeypatch.setattr(api, "_get_openai_key", lambda: None)
    resp = await client.post(
        "/api/voice/transcribe",
        files={"audio": ("test.webm", b"fake_audio_data", "audio/webm")},
    )
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_transcribe_with_mocked_openai(client, monkeypatch):
    """Returns 200 with transcribed text when OpenAI is mocked."""
    monkeypatch.setattr(api, "_get_openai_key", lambda: "sk-test-key")

    # Mock openai.AsyncOpenAI
    mock_transcript = MagicMock()
    mock_transcript.text = "Hello from Whisper"

    mock_transcriptions = MagicMock()
    mock_transcriptions.create = AsyncMock(return_value=mock_transcript)

    mock_audio = MagicMock()
    mock_audio.transcriptions = mock_transcriptions

    mock_openai_client = MagicMock()
    mock_openai_client.audio = mock_audio

    mock_openai_class = MagicMock(return_value=mock_openai_client)

    with patch("openai.AsyncOpenAI", mock_openai_class):
        resp = await client.post(
            "/api/voice/transcribe",
            files={"audio": ("test.webm", b"fake_audio_data", "audio/webm")},
        )

    assert resp.status_code == 200
    assert resp.json()["text"] == "Hello from Whisper"


@pytest.mark.asyncio
async def test_synthesize_no_key(client, monkeypatch):
    """Returns 503 when no OpenAI key configured."""
    monkeypatch.setattr(api, "_get_openai_key", lambda: None)
    resp = await client.post("/api/voice/synthesize", json={"text": "Hello"})
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_synthesize_text_too_long(client, monkeypatch):
    """Text longer than 4096 chars is silently truncated; returns 200."""
    monkeypatch.setattr(api, "_get_openai_key", lambda: "sk-test-key")

    mock_speech = MagicMock()
    mock_speech.content = b"fake_mp3_bytes"

    mock_speech_create = AsyncMock(return_value=mock_speech)
    mock_openai_client = MagicMock()
    mock_openai_client.audio.speech.create = mock_speech_create
    mock_openai_class = MagicMock(return_value=mock_openai_client)

    long_text = "A" * 5000
    with patch("openai.AsyncOpenAI", mock_openai_class):
        resp = await client.post("/api/voice/synthesize", json={"text": long_text})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    # Verify the call truncated the text to 4096
    call_kwargs = mock_speech_create.call_args.kwargs
    assert len(call_kwargs.get("input", "A" * 5000)) <= 4096
