"""
テスト共通フィクスチャ・環境設定

NOTE: main.py はインポート時に環境変数チェックを行うため、
      テスト用のダミー値を pytest が起動する前に設定する必要がある。
      conftest.py の最上部で os.environ に設定することで対応。
"""

import os

# main.py のインポート前に必須環境変数をセット（SEC-004 対応）
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key-xxx")
os.environ.setdefault("DEEPGRAM_API_KEY", "test-deepgram-key-xxx")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-twilio-auth-token")
os.environ.setdefault("PUBLIC_HOST", "test.example.com")
os.environ.setdefault("MAX_CONCURRENT_CALLS", "5")

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ------------------------------------------------------------------ #
#  pytest-asyncio のデフォルトモード設定                                #
# ------------------------------------------------------------------ #
pytest_plugins = ["pytest_asyncio"]


# ------------------------------------------------------------------ #
#  CallSession 用フィクスチャ                                          #
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_websocket():
    """FastAPI WebSocket のモック"""
    ws = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    ws.send_json = AsyncMock()
    ws.iter_text = AsyncMock()
    return ws


@pytest.fixture
def mock_openai_client():
    """AsyncOpenAI クライアントのモック"""
    client = AsyncMock()

    # chat.completions.create のデフォルト応答
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = "はい、整骨院さくらでございます。"
    client.chat.completions.create = AsyncMock(return_value=completion)

    # audio.speech.create のデフォルト応答（PCM データを返す想定）
    tts_response = MagicMock()
    tts_response.content = b"\x00\x01" * 4800  # 24kHz 16bit 0.1秒分のダミーPCM
    client.audio.speech.create = AsyncMock(return_value=tts_response)

    return client


@pytest.fixture
def call_session(mock_websocket, mock_openai_client):
    """テスト用 CallSession インスタンス（外部接続なし）"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from call_handler import CallSession

    session = CallSession(mock_websocket, mock_openai_client)
    session.stream_sid = "MZ_test_stream_sid_001"
    return session
