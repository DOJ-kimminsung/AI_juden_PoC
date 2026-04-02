"""
CallSession のユニットテスト

カバー範囲:
- _process_user_input: 正常系、LLMエラー、会話履歴の切り詰め
- _speak: 正常系、stream_sid未設定、TTSエラー、Lock による排他制御
- _handle_twilio: start/media/stop イベント処理
- _handle_deepgram: Results 解析、空文字・非finalのスキップ
"""

import asyncio
import base64
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from call_handler import CallSession, MAX_HISTORY_TURNS, AUDIO_CHUNK_SIZE


# ------------------------------------------------------------------ #
#  ヘルパー: テスト内で CallSession を生成（イベントループ対応）        #
# ------------------------------------------------------------------ #
def _make_session():
    """asyncio.Lock をテストのイベントループで生成するため、テスト内で呼ぶ"""
    ws = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    ws.send_json = AsyncMock()
    ws.iter_text = AsyncMock()

    client = AsyncMock()
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = "はい、整骨院さくらでございます。"
    client.chat.completions.create = AsyncMock(return_value=completion)

    tts_response = MagicMock()
    tts_response.content = b"\x00\x01" * 4800
    client.audio.speech.create = AsyncMock(return_value=tts_response)

    session = CallSession(ws, client)
    session.stream_sid = "MZ_test_stream_sid_001"
    return session


# ================================================================== #
#  _process_user_input                                                #
# ================================================================== #

class TestProcessUserInput:

    @pytest.mark.asyncio
    async def test_正常系_AI応答を会話履歴に追加してspeakを呼ぶ(self):
        session = _make_session()
        with patch.object(session, "_speak", new_callable=AsyncMock) as mock_speak:
            await session._process_user_input("予約したいのですが")

        assert session.conversation[0] == {"role": "user", "content": "予約したいのですが"}
        assert session.conversation[1]["role"] == "assistant"
        mock_speak.assert_called_once()

    @pytest.mark.asyncio
    async def test_LLMエラー時は例外が外部に漏れない(self):
        session = _make_session()
        session.client.chat.completions.create = AsyncMock(
            side_effect=Exception("OpenAI rate limit exceeded")
        )
        with patch.object(session, "_speak", new_callable=AsyncMock) as mock_speak:
            await session._process_user_input("テスト")

        mock_speak.assert_not_called()
        assert len(session.conversation) == 1  # ユーザー発話のみ追加済み

    @pytest.mark.asyncio
    async def test_会話履歴が上限超過したら古いものを切り詰める(self):
        """PERF-001: 切り詰め後に user + AI 応答が追加されるので最終は limit + 1 以内"""
        session = _make_session()
        limit = MAX_HISTORY_TURNS * 2
        # 上限ちょうど+2件の履歴を事前設定（22件）
        session.conversation = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg_{i}"}
            for i in range(limit + 2)
        ]
        with patch.object(session, "_speak", new_callable=AsyncMock):
            await session._process_user_input("新しいメッセージ")

        # user追加(23件) → 切り詰め(20件) → AI応答追加(21件) なので <= limit + 1
        assert len(session.conversation) <= limit + 1

    @pytest.mark.asyncio
    async def test_履歴が上限未満のときは切り詰めない(self):
        session = _make_session()
        initial_count = 4
        session.conversation = [
            {"role": "user", "content": f"msg_{i}"} for i in range(initial_count)
        ]
        with patch.object(session, "_speak", new_callable=AsyncMock):
            await session._process_user_input("追加メッセージ")

        assert len(session.conversation) == initial_count + 2


# ================================================================== #
#  _speak                                                             #
# ================================================================== #

class TestSpeak:

    @pytest.mark.asyncio
    async def test_正常系_音声チャンクとmarkイベントをTwilioに送信(self):
        session = _make_session()
        dummy_mulaw = b"\xff" * (AUDIO_CHUNK_SIZE * 2 + 100)

        with patch("call_handler.pcm24k_to_mulaw8k", return_value=dummy_mulaw):
            await session._speak("こんにちは")

        sent_calls = session.ws.send_json.call_args_list
        media_calls = [c for c in sent_calls if c[0][0].get("event") == "media"]
        mark_calls  = [c for c in sent_calls if c[0][0].get("event") == "mark"]

        assert len(media_calls) == 3
        assert len(mark_calls) == 1

    @pytest.mark.asyncio
    async def test_stream_sid未設定なら何も送信しない(self):
        session = _make_session()
        session.stream_sid = None
        await session._speak("テスト")
        session.ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_TTSエラー時でもis_ai_speakingがFalseに戻る(self):
        session = _make_session()
        session.client.audio.speech.create = AsyncMock(
            side_effect=Exception("TTS service unavailable")
        )
        await session._speak("テスト")
        assert session.is_ai_speaking is False

    @pytest.mark.asyncio
    async def test_Lockにより2つの同時speak呼び出しが直列化される(self):
        """LOGIC-001: asyncio.Lock で複数の _speak が同時実行されない"""
        session = _make_session()
        execution_order = []

        async def fake_tts(*args, **kwargs):
            execution_order.append("tts_start")
            await asyncio.sleep(0.05)
            execution_order.append("tts_end")
            tts_resp = MagicMock()
            tts_resp.content = b"\x00" * 100
            return tts_resp

        session.client.audio.speech.create = fake_tts

        with patch("call_handler.pcm24k_to_mulaw8k", return_value=b"\x00" * 100):
            await asyncio.gather(
                session._speak("最初のメッセージ"),
                session._speak("2番目のメッセージ"),
            )

        # start→end が必ず連続する（交互にならない）
        assert execution_order == ["tts_start", "tts_end", "tts_start", "tts_end"]

    @pytest.mark.asyncio
    async def test_speak後にis_ai_speakingがFalseになる(self):
        session = _make_session()
        with patch("call_handler.pcm24k_to_mulaw8k", return_value=b"\xff" * 100):
            await session._speak("テスト")
        assert session.is_ai_speaking is False


# ================================================================== #
#  _handle_twilio                                                     #
# ================================================================== #

class TestHandleTwilio:

    def _make_event(self, event_type: str, **kwargs) -> str:
        if event_type == "start":
            return json.dumps({
                "event": "start",
                "start": {"streamSid": kwargs.get("stream_sid", "MZ_test_001")},
            })
        elif event_type == "media":
            payload = base64.b64encode(kwargs.get("audio", b"\x00" * 10)).decode()
            return json.dumps({"event": "media", "media": {"payload": payload}})
        elif event_type == "stop":
            return json.dumps({"event": "stop"})
        return json.dumps({"event": event_type})

    def _make_async_iter(self, items):
        """リストを非同期イテレータに変換するヘルパー"""
        async def _aiter():
            for item in items:
                yield item
        return _aiter()

    @pytest.mark.asyncio
    async def test_startイベントでstream_sidをセットして挨拶を再生(self):
        session = _make_session()
        events = [self._make_event("start", stream_sid="MZ_abc123"), self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))

        with patch.object(session, "_speak", new_callable=AsyncMock) as mock_speak:
            await session._handle_twilio()

        assert session.stream_sid == "MZ_abc123"
        mock_speak.assert_called_once()

    @pytest.mark.asyncio
    async def test_mediaイベントはAI非発話中にDeepgramへ転送(self):
        session = _make_session()
        audio_data = b"\xaa" * 20
        events = [self._make_event("media", audio=audio_data), self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))
        session.is_ai_speaking = False
        session.deepgram_ws = AsyncMock()

        await session._handle_twilio()

        session.deepgram_ws.send.assert_called_once_with(audio_data)

    @pytest.mark.asyncio
    async def test_mediaイベントはAI発話中はDeepgramへ転送しない(self):
        session = _make_session()
        events = [self._make_event("media"), self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))
        session.is_ai_speaking = True
        session.deepgram_ws = AsyncMock()

        await session._handle_twilio()

        session.deepgram_ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_stopイベントでループを終了(self):
        session = _make_session()
        events = [self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))

        await asyncio.wait_for(session._handle_twilio(), timeout=1.0)


# ================================================================== #
#  _handle_deepgram                                                   #
# ================================================================== #

class TestHandleDeepgram:

    def _make_result(self, transcript: str, speech_final: bool = True) -> str:
        return json.dumps({
            "type": "Results",
            "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": transcript}]},
        })

    def _make_deepgram_ws(self, messages):
        """Deepgram WebSocket の非同期イテレータモック

        AsyncMock の __aiter__ は Python 3.9 でバインドの問題があるため、
        __aiter__ / __anext__ を実装した専用クラスを使用する。
        """
        class _AsyncIter:
            def __init__(self, items):
                self._iter = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        return _AsyncIter(messages)

    @pytest.mark.asyncio
    async def test_確定発話でprocess_user_inputを呼ぶ(self):
        session = _make_session()
        session.deepgram_ws = self._make_deepgram_ws(
            [self._make_result("営業時間を教えてください", speech_final=True)]
        )
        with patch.object(session, "_process_user_input", new_callable=AsyncMock) as mock_proc:
            await session._handle_deepgram()

        mock_proc.assert_called_once_with("営業時間を教えてください")

    @pytest.mark.asyncio
    async def test_空文字のtranscriptは無視する(self):
        session = _make_session()
        session.deepgram_ws = self._make_deepgram_ws(
            [self._make_result("", speech_final=True)]
        )
        with patch.object(session, "_process_user_input", new_callable=AsyncMock) as mock_proc:
            await session._handle_deepgram()

        mock_proc.assert_not_called()

    @pytest.mark.asyncio
    async def test_speech_final_Falseの中間結果は無視する(self):
        session = _make_session()
        session.deepgram_ws = self._make_deepgram_ws(
            [self._make_result("こんに", speech_final=False)]
        )
        with patch.object(session, "_process_user_input", new_callable=AsyncMock) as mock_proc:
            await session._handle_deepgram()

        mock_proc.assert_not_called()

    @pytest.mark.asyncio
    async def test_不正JSONは無視して処理継続(self):
        session = _make_session()
        session.deepgram_ws = self._make_deepgram_ws([
            "not-a-json-string",
            self._make_result("有効な発話", speech_final=True),
        ])
        with patch.object(session, "_process_user_input", new_callable=AsyncMock) as mock_proc:
            await session._handle_deepgram()

        mock_proc.assert_called_once_with("有効な発話")
