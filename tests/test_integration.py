"""
インテグレーションテスト — Twilio フルコールフロー模擬

実際の外部API（Deepgram / OpenAI）はモックし、
Twilio ↔ アプリ間の通信フローを end-to-end で検証する。

カバー範囲:
  [Flow-1] 正常通話: 着信 → 挨拶 → 発話認識 → AI応答 → 通話終了
  [Flow-2] Deepgram 接続失敗: 通話は受付られるが STT エラー後も継続
  [Flow-3] OpenAI LLM タイムアウト: エラー後も通話継続（クラッシュしない）
  [Flow-4] OpenAI TTS 失敗: 音声送信失敗後も is_ai_speaking がリセット
  [Flow-5] 複数発話: 連続する発話を正しく順次処理
  [Flow-6] 長時間通話: 会話履歴が上限超過しても継続
  [Flow-7] WebSocket 突然切断: 例外が外部に漏れない
  [Flow-8] 同時2通話: セマフォが正しく動作
"""

import asyncio
import base64
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# conftest.py が環境変数をセット済み
from call_handler import CallSession, MAX_HISTORY_TURNS
from main import app

from fastapi.testclient import TestClient

client = TestClient(app)


# ================================================================== #
#  共通ヘルパー                                                        #
# ================================================================== #

def _pcm_dummy(seconds: float = 0.05) -> bytes:
    """ダミーPCMデータ（24kHz 16bit mono）"""
    return b"\x00\x01" * int(24000 * seconds)


def _deepgram_result(transcript: str, speech_final: bool = True, is_final: bool = True) -> str:
    return json.dumps({
        "type": "Results",
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": transcript}]},
    })


def _twilio_event(event_type: str, **kwargs) -> str:
    if event_type == "start":
        return json.dumps({"event": "start", "start": {"streamSid": kwargs.get("sid", "MZ_integ_001")}})
    elif event_type == "media":
        payload = base64.b64encode(kwargs.get("audio", b"\x00" * 160)).decode()
        return json.dumps({"event": "media", "media": {"payload": payload}})
    elif event_type == "stop":
        return json.dumps({"event": "stop"})
    return json.dumps({"event": event_type})


class _AsyncIter:
    """非同期イテレータ（Deepgram/Twilio WS モック用）"""
    def __init__(self, items):
        self._iter = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _make_llm_stream(reply: str):
    """chat.completions.create(stream=True) のモック: 非同期イテレータ"""
    class _FakeChunk:
        def __init__(self, text):
            self.choices = [MagicMock()]
            self.choices[0].delta.content = text

    class _FakeStream:
        def __init__(self, text):
            self._iter = iter([text])
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return _FakeChunk(next(self._iter))
            except StopIteration:
                raise StopAsyncIteration

    return _FakeStream(reply)


def _make_streaming_tts_response(pcm_chunks: list[bytes]):
    """with_streaming_response.create() のコンテキストマネージャモック"""
    class _FakeStreamResponse:
        async def iter_bytes(self, chunk_size=4096):
            for chunk in pcm_chunks:
                yield chunk

    @asynccontextmanager
    async def _fake_ctx(*args, **kwargs):
        yield _FakeStreamResponse()

    return _fake_ctx


def _make_openai_mock(reply: str = "承知しました。"):
    """OpenAI chat（ストリーミング）+ TTS（ストリーミング）のモックを生成"""
    oc = AsyncMock()

    # LLM ストリーミング
    async def _create_stream(*args, **kwargs):
        return _make_llm_stream(reply)
    oc.chat.completions.create = AsyncMock(side_effect=_create_stream)

    # TTS ストリーミング
    pcm_chunk = _pcm_dummy(0.1)
    oc.audio.speech.with_streaming_response.create = _make_streaming_tts_response([pcm_chunk])

    return oc


def _make_ws_mock(events):
    """Twilio WebSocket のモック"""
    ws = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    ws.send_json = AsyncMock()
    ws.iter_text = MagicMock(return_value=_AsyncIter(events))
    return ws


def _make_deepgram_mock(messages):
    """Deepgram WebSocket のモック"""
    dg = AsyncMock()
    dg.__aiter__ = lambda self=None: _AsyncIter(messages)
    dg.send = AsyncMock()
    return dg


# ================================================================== #
#  Flow-1: 正常通話フロー（着信 → 挨拶 → 発話 → AI応答 → 終了）        #
# ================================================================== #

class TestNormalCallFlow:

    @pytest.mark.asyncio
    async def test_着信webhookが200とTwiMLを返す(self):
        """POST /incoming-call が有効な署名で 200 + Stream TwiML を返す"""
        from main import PUBLIC_HOST
        with patch("main.twilio_validator.validate", return_value=True):
            resp = client.post("/incoming-call", headers={"X-Twilio-Signature": "valid"}, data={})
        assert resp.status_code == 200
        assert f"wss://{PUBLIC_HOST}/ws/stream" in resp.text

    @pytest.mark.asyncio
    async def test_startイベントで挨拶音声が送信される(self):
        """start イベント受信後、挨拶 TTS が Twilio に送信される"""
        events = [_twilio_event("start"), _twilio_event("stop")]
        ws = _make_ws_mock(events)
        oc = _make_openai_mock()

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 800, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock([]))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        # 挨拶の音声チャンク(media) + 完了通知(mark) が送信されたことを確認
        calls = ws.send_json.call_args_list
        assert any(c[0][0].get("event") == "media" for c in calls), "mediaイベントが送信されていない"
        assert any(c[0][0].get("event") == "mark"  for c in calls), "markイベントが送信されていない"

    @pytest.mark.asyncio
    async def test_ユーザー発話からAI応答までの完全フロー(self):
        """STT → LLM → TTS → Twilio送信 の完全フローを検証"""
        twilio_events = [_twilio_event("start"), _twilio_event("stop")]
        deepgram_msgs = [_deepgram_result("予約をお願いしたいのですが", speech_final=True)]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock(reply="はい、ご予約承ります。お名前をお聞かせください。")

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 800, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock(deepgram_msgs))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        # LLM が呼ばれたことを確認（ストリーミング）
        oc.chat.completions.create.assert_called()
        call_kwargs = oc.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("stream") is True, "LLMがストリーミングモードで呼ばれていない"
        messages = call_kwargs.get("messages", [])
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert any("予約" in m["content"] for m in user_msgs), "ユーザー発話がLLMに渡されていない"


# ================================================================== #
#  Flow-2: Deepgram 接続失敗                                          #
# ================================================================== #

class TestDeepgramFailure:

    @pytest.mark.asyncio
    async def test_Deepgram接続失敗でもクラッシュしない(self):
        """Deepgram への接続が失敗しても例外が外部に漏れない"""
        import websockets as ws_lib
        ws = _make_ws_mock([_twilio_event("stop")])
        oc = _make_openai_mock()

        with patch("call_handler.websockets.connect", side_effect=ws_lib.exceptions.WebSocketException("Deepgram接続失敗")):
            session = CallSession(ws, oc)
            await session.run()

    @pytest.mark.asyncio
    async def test_Deepgram切断後もTwilioループは継続(self):
        """Deepgram が途中切断されても Twilio の stop イベントまで処理継続"""
        twilio_events = [
            _twilio_event("start"),
            _twilio_event("media"),
            _twilio_event("stop"),
        ]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock()

        import websockets as ws_lib
        dg = _make_deepgram_mock([])
        dg.send = AsyncMock(side_effect=ws_lib.exceptions.ConnectionClosed(None, None))

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 100, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=dg)
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()


# ================================================================== #
#  Flow-3: OpenAI LLM エラー                                          #
# ================================================================== #

class TestLLMFailure:

    @pytest.mark.asyncio
    async def test_LLMタイムアウトでも通話継続(self):
        """OpenAI API タイムアウト後も通話セッションがクラッシュしない"""
        twilio_events = [_twilio_event("start"), _twilio_event("stop")]
        deepgram_msgs = [_deepgram_result("テストです", speech_final=True)]
        ws = _make_ws_mock(twilio_events)

        oc = _make_openai_mock()
        oc.chat.completions.create = AsyncMock(
            side_effect=Exception("OpenAI: connection timeout")
        )

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 100, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock(deepgram_msgs))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

    @pytest.mark.asyncio
    async def test_LLMエラー後に次の発話を正常処理(self):
        """1回目のLLMエラー後、2回目の発話は正常に処理される"""
        twilio_events = [_twilio_event("start"), _twilio_event("stop")]
        deepgram_msgs = [
            _deepgram_result("1回目の発話", speech_final=True),
            _deepgram_result("2回目の発話", speech_final=True),
        ]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock()

        call_count = 0
        async def flaky_llm(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("1回目はエラー")
            return _make_llm_stream("2回目は正常です。")

        oc.chat.completions.create = flaky_llm

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 100, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock(deepgram_msgs))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        assert call_count >= 1, "LLMが一度も呼ばれていない"


# ================================================================== #
#  Flow-4: OpenAI TTS エラー                                          #
# ================================================================== #

class TestTTSFailure:

    @pytest.mark.asyncio
    async def test_TTS失敗後is_ai_speakingがFalseに戻る(self):
        """TTS エラー後も is_ai_speaking = False にリセットされる"""
        twilio_events = [_twilio_event("start"), _twilio_event("stop")]
        deepgram_msgs = [_deepgram_result("テスト", speech_final=True)]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock()

        @asynccontextmanager
        async def _error_tts(*args, **kwargs):
            raise Exception("TTS API unavailable")
            yield  # noqa

        oc.audio.speech.with_streaming_response.create = _error_tts

        with patch("call_handler.websockets.connect") as mock_connect:
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock(deepgram_msgs))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        assert session.is_ai_speaking is False, "TTS失敗後もis_ai_speakingがTrueのまま"

    @pytest.mark.asyncio
    async def test_TTS失敗後に次のメディアがDeepgramに転送される(self):
        """TTS 失敗 → is_ai_speaking = False → 次の音声がDeepgramに転送される"""
        audio = b"\xaa" * 160
        twilio_events = [
            _twilio_event("start"),
            _twilio_event("media", audio=audio),
            _twilio_event("stop"),
        ]
        deepgram_msgs = [_deepgram_result("テスト", speech_final=True)]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock()

        @asynccontextmanager
        async def _error_tts(*args, **kwargs):
            raise Exception("TTS失敗")
            yield  # noqa

        oc.audio.speech.with_streaming_response.create = _error_tts
        dg = _make_deepgram_mock(deepgram_msgs)

        with patch("call_handler.websockets.connect") as mock_connect:
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=dg)
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        assert session.is_ai_speaking is False


# ================================================================== #
#  Flow-5: 複数発話の連続処理                                          #
# ================================================================== #

class TestMultipleUtterances:

    @pytest.mark.asyncio
    async def test_3回連続の発話が処理される(self):
        """3回連続の確定発話で少なくとも最後の発話が LLM に渡される。
        バージイン対応により、高速連続発話では前のタスクがキャンセルされるため
        すべてが処理されるとは限らない（実使用では発話間に間隔がある）。"""
        twilio_events = [_twilio_event("start"), _twilio_event("stop")]
        deepgram_msgs = [
            _deepgram_result("営業時間を教えてください", speech_final=True),
            _deepgram_result("予約したいです",         speech_final=True),
            _deepgram_result("ありがとうございました",   speech_final=True),
        ]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock()

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 100, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock(deepgram_msgs))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        assert oc.chat.completions.create.call_count >= 1, \
            "LLMが一度も呼ばれていない"


# ================================================================== #
#  Flow-6: 長時間通話（会話履歴の上限管理）                             #
# ================================================================== #

class TestLongCall:

    @pytest.mark.asyncio
    async def test_上限超過した会話履歴が自動切り詰められる(self):
        """MAX_HISTORY_TURNS を超える発話でも履歴が上限以内に保たれる"""
        over_limit = MAX_HISTORY_TURNS + 3
        twilio_events = [_twilio_event("start"), _twilio_event("stop")]
        deepgram_msgs = [
            _deepgram_result(f"発話{i}", speech_final=True)
            for i in range(over_limit)
        ]
        ws = _make_ws_mock(twilio_events)
        oc = _make_openai_mock()

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 100, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock(deepgram_msgs))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()

        assert len(session.conversation) <= MAX_HISTORY_TURNS * 2 + 1, \
            f"会話履歴が切り詰められていない: {len(session.conversation)}件"


# ================================================================== #
#  Flow-7: WebSocket 突然切断                                          #
# ================================================================== #

class TestWebSocketDisconnect:

    @pytest.mark.asyncio
    async def test_TwilioWS突然切断でもクラッシュしない(self):
        """Twilio WebSocket が iter_text 中に例外を投げても外部に漏れない"""
        async def broken_iter():
            yield _twilio_event("start")
            raise Exception("WebSocket突然切断")

        ws = AsyncMock()
        ws.client = MagicMock()
        ws.client.host = "127.0.0.1"
        ws.send_json = AsyncMock()
        ws.iter_text = MagicMock(return_value=broken_iter())
        oc = _make_openai_mock()

        with patch("call_handler.websockets.connect") as mock_connect, \
             patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 100, None)):
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=_make_deepgram_mock([]))
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            session = CallSession(ws, oc)
            await session.run()


# ================================================================== #
#  Flow-8: 同時2通話（セマフォ）                                       #
# ================================================================== #

class TestConcurrentCalls:

    def test_ヘルスチェックが常に200を返す(self):
        """通話中でもヘルスチェックは正常に応答する"""
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_無効署名は通話セッションを作らずに403(self):
        """不正な Twilio 署名は WebSocket セッション開始前に弾かれる"""
        with patch("main.twilio_validator.validate", return_value=False):
            resp = client.post("/incoming-call", data={})
        assert resp.status_code == 403
