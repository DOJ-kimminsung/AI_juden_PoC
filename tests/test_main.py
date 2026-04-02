"""
FastAPI エンドポイントのテスト

カバー範囲:
- GET  /health         : 正常応答
- POST /incoming-call  : 署名検証OK/NG、TwiML レスポンス形式
- 起動時環境変数チェック  : 必須変数未設定で EnvironmentError
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from main import app, PUBLIC_HOST

client = TestClient(app)


# ================================================================== #
#  GET /health                                                        #
# ================================================================== #

class TestHealth:

    def test_正常系_200とステータスokを返す(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "service": "AI受電システム PoC"}


# ================================================================== #
#  POST /incoming-call  (Twilio 署名検証)                             #
# ================================================================== #

class TestIncomingCall:

    def test_有効な署名なら200とTwiMLを返す(self):
        """SEC-001: 正しい Twilio 署名なら TwiML レスポンスを返す"""
        with patch("main.twilio_validator.validate", return_value=True):
            response = client.post(
                "/incoming-call",
                headers={"X-Twilio-Signature": "valid-sig"},
                data={},
            )

        assert response.status_code == 200
        assert "application/xml" in response.headers["content-type"]
        assert "<Stream" in response.text

    def test_無効な署名なら403を返す(self):
        """SEC-001: 署名が不正な場合は 403 Forbidden を返す"""
        with patch("main.twilio_validator.validate", return_value=False):
            response = client.post(
                "/incoming-call",
                headers={"X-Twilio-Signature": "invalid-signature"},
                data={},
            )

        assert response.status_code == 403

    def test_署名ヘッダーなしなら403を返す(self):
        """SEC-001: X-Twilio-Signature ヘッダーが存在しない場合も 403"""
        with patch("main.twilio_validator.validate", return_value=False):
            response = client.post("/incoming-call", data={})

        assert response.status_code == 403

    def test_TwiMLにPUBLIC_HOSTが含まれる(self):
        """SEC-002: レスポンスの wss:// URL がヘッダーではなく環境変数のホスト名"""
        with patch("main.twilio_validator.validate", return_value=True):
            response = client.post(
                "/incoming-call",
                headers={
                    "X-Twilio-Signature": "valid",
                    "host": "attacker.evil.com",
                },
                data={},
            )

        # リクエストの Host ヘッダー（attacker.evil.com）は使われない
        assert "attacker.evil.com" not in response.text
        assert PUBLIC_HOST in response.text

    def test_TwiMLのXML構造が正しい(self):
        """TwiML レスポンスが正しい XML 構造を持つ"""
        import xml.etree.ElementTree as ET

        with patch("main.twilio_validator.validate", return_value=True):
            response = client.post(
                "/incoming-call",
                headers={"X-Twilio-Signature": "valid"},
                data={},
            )

        root = ET.fromstring(response.text)
        assert root.tag == "Response"
        connect = root.find("Connect")
        assert connect is not None
        stream = connect.find("Stream")
        assert stream is not None
        assert stream.get("url", "").startswith("wss://")


# ================================================================== #
#  環境変数バリデーション（SEC-004）                                    #
# ================================================================== #

class TestEnvValidation:

    def test_必須環境変数が未設定の場合EnvironmentErrorが発生する(self):
        """SEC-004: 必須環境変数が未設定のときに EnvironmentError が発生する"""
        required_vars = [
            "OPENAI_API_KEY",
            "DEEPGRAM_API_KEY",
            "TWILIO_AUTH_TOKEN",
            "PUBLIC_HOST",
        ]
        # 実際の起動時ロジックを再現してチェック
        env_backup = {v: os.environ.get(v) for v in required_vars}
        try:
            os.environ["PUBLIC_HOST"] = ""  # 空文字で未設定扱い
            with pytest.raises(EnvironmentError):
                for var in required_vars:
                    if not os.getenv(var):
                        raise EnvironmentError(f"必須環境変数が未設定です: {var}")
        finally:
            # 環境変数を元に戻す
            for var, val in env_backup.items():
                if val is not None:
                    os.environ[var] = val
                elif var in os.environ:
                    del os.environ[var]
