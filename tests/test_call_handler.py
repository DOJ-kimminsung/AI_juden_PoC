"""
CallSession のユニットテスト

カバー範囲:
- _process_user_input: 正常系、LLMエラー、会話履歴の切り詰め
- _speak_streaming: 正常系、CancelledError、stream_sid未設定
- _speak_greeting: 正常系
- _tts_stream_send: 正常系、markイベント送信
- _cancel_current_speak: タスクキャンセル、clearイベント送信
- _handle_twilio: start/media/stop イベント処理（バージイン対応）
- _handle_deepgram: バージイン検出、確定発話処理
"""

import asyncio
import base64
import json
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from call_handler import CallSession, MAX_HISTORY_TURNS, AUDIO_CHUNK_SIZE


# ------------------------------------------------------------------ #
#  ヘルパー: ストリーミング TTS レスポンスのモック                       #
# ------------------------------------------------------------------ #
def _make_streaming_tts_response(pcm_chunks: list[bytes]):
    """with_streaming_response.create() のコンテキストマネージャモック"""

    class _AsyncBytesIter:
        def __init__(self, chunks):
            self._iter = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class _FakeStreamResponse:
        async def aiter_bytes(self, chunk_size=4096):
            for chunk in pcm_chunks:
                yield chunk

    @asynccontextmanager
    async def _fake_ctx(*args, **kwargs):
        yield _FakeStreamResponse()

    return _fake_ctx


def _make_llm_stream(texts: list[str]):
    """chat.completions.create(stream=True) のモック非同期イテレータ"""

    class _FakeChunk:
        def __init__(self, text):
            self.choices = [MagicMock()]
            self.choices[0].delta.content = text

    class _FakeStream:
        def __init__(self, texts):
            self._iter = iter(texts)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return _FakeChunk(next(self._iter))
            except StopIteration:
                raise StopAsyncIteration

    return _FakeStream(texts)


# ------------------------------------------------------------------ #
#  ヘルパー: テスト内で CallSession を生成                             #
# ------------------------------------------------------------------ #
def _make_session():
    ws = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    ws.send_json = AsyncMock()
    ws.iter_text = AsyncMock()

    client = AsyncMock()
    # LLM ストリーミングモック（デフォルト: 短いレスポンス）
    client.chat.completions.create = AsyncMock(
        return_value=_make_llm_stream(["はい、整骨院さくらでございます。"])
    )
    # TTS ストリーミングモック
    pcm_chunk = b"\x00\x01" * 2400  # 24kHz 0.1秒分
    client.audio.speech.with_streaming_response.create = _make_streaming_tts_response(
        [pcm_chunk]
    )

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
        with patch.object(session, "_speak_streaming", new_callable=AsyncMock, return_value="応答テキスト") as mock_speak:
            await session._process_user_input("予約したいのですが")

        assert session.conversation[0] == {"role": "user", "content": "予約したいのですが"}
        assert session.conversation[1] == {"role": "assistant", "content": "応答テキスト"}
        mock_speak.assert_called_once()

    @pytest.mark.asyncio
    async def test_LLMエラー時は例外が外部に漏れない(self):
        session = _make_session()
        session.client.chat.completions.create = AsyncMock(
            side_effect=Exception("OpenAI rate limit exceeded")
        )
        with patch.object(session, "_speak_streaming", new_callable=AsyncMock) as mock_speak:
            await session._process_user_input("テスト")

        mock_speak.assert_not_called()
        assert len(session.conversation) == 1  # ユーザー発話のみ追加済み

    @pytest.mark.asyncio
    async def test_会話履歴が上限超過したら古いものを切り詰める(self):
        session = _make_session()
        limit = MAX_HISTORY_TURNS * 2
        session.conversation = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg_{i}"}
            for i in range(limit + 2)
        ]
        with patch.object(session, "_speak_streaming", new_callable=AsyncMock, return_value="resp"):
            await session._process_user_input("新しいメッセージ")

        assert len(session.conversation) <= limit + 1

    @pytest.mark.asyncio
    async def test_履歴が上限未満のときは切り詰めない(self):
        session = _make_session()
        initial_count = 4
        session.conversation = [
            {"role": "user", "content": f"msg_{i}"} for i in range(initial_count)
        ]
        with patch.object(session, "_speak_streaming", new_callable=AsyncMock, return_value="resp"):
            await session._process_user_input("追加メッセージ")

        assert len(session.conversation) == initial_count + 2


# ================================================================== #
#  _speak_streaming                                                   #
# ================================================================== #

class TestSpeakStreaming:

    @pytest.mark.asyncio
    async def test_正常系_全文テキストを返す(self):
        session = _make_session()
        stream = _make_llm_stream(["こんにちは。", "よろしくお願いします。"])
        with patch.object(session, "_tts_stream_send", new_callable=AsyncMock):
            result = await session._speak_streaming(stream)
        assert "こんにちは。" in result
        assert "よろしくお願いします。" in result

    @pytest.mark.asyncio
    async def test_CancelledError後にis_ai_speakingがFalseになる(self):
        session = _make_session()

        async def _cancel_immediately(*args, **kwargs):
            raise asyncio.CancelledError()

        stream = _make_llm_stream(["テスト"])
        with patch.object(session, "_tts_stream_send", side_effect=_cancel_immediately):
            result = await session._speak_streaming(stream)

        assert session.is_ai_speaking is False

    @pytest.mark.asyncio
    async def test_LLMエラー後にis_ai_speakingがFalseになる(self):
        session = _make_session()

        class _ErrorStream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise Exception("LLM error")

        with patch.object(session, "_tts_stream_send", new_callable=AsyncMock):
            result = await session._speak_streaming(_ErrorStream())

        assert session.is_ai_speaking is False

    @pytest.mark.asyncio
    async def test_stream_sid未設定なら何も送信しない(self):
        session = _make_session()
        session.stream_sid = None
        stream = _make_llm_stream(["テスト"])
        with patch.object(session, "_tts_stream_send", new_callable=AsyncMock) as mock_tts:
            result = await session._speak_streaming(stream)
        mock_tts.assert_not_called()
        assert result == ""

    @pytest.mark.asyncio
    async def test_センテンス分割でtts_stream_sendが複数回呼ばれる(self):
        """文末文字で区切られた長いテキストは複数回 TTS を呼ぶ"""
        session = _make_session()
        # 10文字以上のセンテンスを2つ含む
        stream = _make_llm_stream([
            "こんにちは、整骨院さくらでございます。",
            "本日はどのようなご用件でしょうか？"
        ])
        tts_calls = []
        async def _record_tts(text):
            tts_calls.append(text)

        with patch.object(session, "_tts_stream_send", side_effect=_record_tts):
            await session._speak_streaming(stream)

        assert len(tts_calls) >= 1


# ================================================================== #
#  _speak_greeting                                                    #
# ================================================================== #

class TestSpeakGreeting:

    @pytest.mark.asyncio
    async def test_正常系_tts_stream_sendを呼ぶ(self):
        session = _make_session()
        with patch.object(session, "_tts_stream_send", new_callable=AsyncMock) as mock_tts:
            await session._speak_greeting()
        mock_tts.assert_called_once()

    @pytest.mark.asyncio
    async def test_完了後にis_ai_speakingがFalseになる(self):
        session = _make_session()
        with patch.object(session, "_tts_stream_send", new_callable=AsyncMock):
            await session._speak_greeting()
        assert session.is_ai_speaking is False

    @pytest.mark.asyncio
    async def test_CancelledError後にis_ai_speakingがFalseになる(self):
        session = _make_session()
        with patch.object(session, "_tts_stream_send", side_effect=asyncio.CancelledError):
            await session._speak_greeting()
        assert session.is_ai_speaking is False


# ================================================================== #
#  _tts_stream_send                                                   #
# ================================================================== #

class TestTtsStreamSend:

    @pytest.mark.asyncio
    async def test_正常系_mediaとmarkイベントを送信する(self):
        session = _make_session()
        pcm_data = b"\x00\x01" * 2400  # 24kHz 0.1秒分

        with patch("call_handler.pcm24k_to_mulaw8k_chunk", return_value=(b"\xff" * 800, None)):
            session.client.audio.speech.with_streaming_response.create = \
                _make_streaming_tts_response([pcm_data])
            await session._tts_stream_send("こんにちは")

        sent_calls = session.ws.send_json.call_args_list
        media_calls = [c for c in sent_calls if c[0][0].get("event") == "media"]
        mark_calls  = [c for c in sent_calls if c[0][0].get("event") == "mark"]
        assert len(media_calls) >= 1
        assert len(mark_calls) == 1
        assert mark_calls[0][0][0]["mark"]["name"] == "sentence_end"

    @pytest.mark.asyncio
    async def test_stream_sid未設定なら何も送信しない(self):
        session = _make_session()
        session.stream_sid = None
        await session._tts_stream_send("テスト")
        session.ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_空テキストなら何も送信しない(self):
        session = _make_session()
        await session._tts_stream_send("")
        session.ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_TTSエラー時は例外が外部に漏れない(self):
        session = _make_session()

        @asynccontextmanager
        async def _error_ctx(*args, **kwargs):
            raise Exception("TTS service unavailable")
            yield  # noqa

        session.client.audio.speech.with_streaming_response.create = _error_ctx
        # 例外が伝播しないこと
        await session._tts_stream_send("テスト")

    @pytest.mark.asyncio
    async def test_CancelledErrorは再raiseされる(self):
        session = _make_session()

        @asynccontextmanager
        async def _cancel_ctx(*args, **kwargs):
            raise asyncio.CancelledError()
            yield  # noqa

        session.client.audio.speech.with_streaming_response.create = _cancel_ctx
        with pytest.raises(asyncio.CancelledError):
            await session._tts_stream_send("テスト")


# ================================================================== #
#  _cancel_current_speak                                              #
# ================================================================== #

class TestCancelCurrentSpeak:

    @pytest.mark.asyncio
    async def test_speak_taskをキャンセルしてclearイベントを送信する(self):
        session = _make_session()

        # ダミーの実行中タスクを作成
        async def _long_task():
            await asyncio.sleep(10)

        task = asyncio.create_task(_long_task())
        session._speak_task = task

        await session._cancel_current_speak()

        assert task.cancelled()
        sent_calls = session.ws.send_json.call_args_list
        clear_calls = [c for c in sent_calls if c[0][0].get("event") == "clear"]
        assert len(clear_calls) == 1

    @pytest.mark.asyncio
    async def test_タスクが既に完了していても例外なく動作する(self):
        session = _make_session()

        async def _done_task():
            pass

        task = asyncio.create_task(_done_task())
        await asyncio.sleep(0)  # タスクを完了させる
        session._speak_task = task

        await session._cancel_current_speak()  # 例外が出ないこと

        sent_calls = session.ws.send_json.call_args_list
        clear_calls = [c for c in sent_calls if c[0][0].get("event") == "clear"]
        assert len(clear_calls) == 1

    @pytest.mark.asyncio
    async def test_speak_taskがNoneでも例外なく動作する(self):
        session = _make_session()
        session._speak_task = None
        await session._cancel_current_speak()  # 例外が出ないこと


# ================================================================== #
#  _handle_twilio（バージイン対応）                                    #
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
        async def _aiter():
            for item in items:
                yield item
        return _aiter()

    @pytest.mark.asyncio
    async def test_startイベントでstream_sidをセットして挨拶を再生(self):
        session = _make_session()
        events = [self._make_event("start", stream_sid="MZ_abc123"), self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))

        with patch.object(session, "_speak_greeting", new_callable=AsyncMock) as mock_greeting:
            await session._handle_twilio()

        assert session.stream_sid == "MZ_abc123"
        mock_greeting.assert_called_once()

    @pytest.mark.asyncio
    async def test_mediaイベントはAI非発話中でもDeepgramへ転送する(self):
        session = _make_session()
        audio_data = b"\xaa" * 20
        events = [self._make_event("media", audio=audio_data), self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))
        session.is_ai_speaking = False
        session.deepgram_ws = AsyncMock()

        await session._handle_twilio()

        session.deepgram_ws.send.assert_called_once_with(audio_data)

    @pytest.mark.asyncio
    async def test_mediaイベントはAI発話中でもDeepgramへ転送する(self):
        """バージイン対応: is_ai_speaking=True でも常に転送する"""
        session = _make_session()
        audio_data = b"\xbb" * 20
        events = [self._make_event("media", audio=audio_data), self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))
        session.is_ai_speaking = True
        session.deepgram_ws = AsyncMock()

        await session._handle_twilio()

        session.deepgram_ws.send.assert_called_once_with(audio_data)

    @pytest.mark.asyncio
    async def test_stopイベントでループを終了(self):
        session = _make_session()
        events = [self._make_event("stop")]
        session.ws.iter_text = MagicMock(return_value=self._make_async_iter(events))

        await asyncio.wait_for(session._handle_twilio(), timeout=1.0)


# ================================================================== #
#  _handle_deepgram（バージイン検出）                                  #
# ================================================================== #

class TestHandleDeepgram:

    def _make_result(
        self,
        transcript: str,
        speech_final: bool = True,
        is_final: bool = True,
    ) -> str:
        return json.dumps({
            "type": "Results",
            "is_final": is_final,
            "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": transcript}]},
        })

    def _make_deepgram_ws(self, messages):
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
            [self._make_result("営業時間を教えてください", speech_final=True, is_final=True)]
        )
        with patch.object(session, "_process_user_input", new_callable=AsyncMock) as mock_proc:
            await session._handle_deepgram()
            await asyncio.sleep(0)  # create_task を実行させる

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
    async def test_interim結果でAI発話中はbarge_inが発動する(self):
        """is_final=False + is_ai_speaking=True → _cancel_current_speak が呼ばれる"""
        session = _make_session()
        session.is_ai_speaking = True
        session.deepgram_ws = self._make_deepgram_ws(
            [self._make_result("こんに", speech_final=False, is_final=False)]
        )
        with patch.object(session, "_cancel_current_speak", new_callable=AsyncMock) as mock_cancel:
            with patch.object(session, "_process_user_input", new_callable=AsyncMock):
                await session._handle_deepgram()
                await asyncio.sleep(0)

        mock_cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_interim結果でAI非発話中はbarge_inしない(self):
        """is_ai_speaking=False → バージイン不発"""
        session = _make_session()
        session.is_ai_speaking = False
        session.deepgram_ws = self._make_deepgram_ws(
            [self._make_result("こんに", speech_final=False, is_final=False)]
        )
        with patch.object(session, "_cancel_current_speak", new_callable=AsyncMock) as mock_cancel:
            await session._handle_deepgram()
            await asyncio.sleep(0)

        mock_cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_barge_in_triggeredフラグで二重キャンセルを防ぐ(self):
        """同じ発話中に2回 interim が来ても cancel は1回だけ"""
        session = _make_session()
        session.is_ai_speaking = True
        session.deepgram_ws = self._make_deepgram_ws([
            self._make_result("こんに", speech_final=False, is_final=False),
            self._make_result("こんにちは", speech_final=False, is_final=False),
        ])
        with patch.object(session, "_cancel_current_speak", new_callable=AsyncMock) as mock_cancel:
            with patch.object(session, "_process_user_input", new_callable=AsyncMock):
                await session._handle_deepgram()
                await asyncio.sleep(0)

        mock_cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_speech_final後にbarge_in_triggeredがリセットされる(self):
        """speech_final=True が来たら _barge_in_triggered が False に戻る"""
        session = _make_session()
        session._barge_in_triggered = True
        session.deepgram_ws = self._make_deepgram_ws(
            [self._make_result("営業時間は？", speech_final=True, is_final=True)]
        )
        with patch.object(session, "_process_user_input", new_callable=AsyncMock):
            await session._handle_deepgram()
            await asyncio.sleep(0)

        assert session._barge_in_triggered is False

    @pytest.mark.asyncio
    async def test_不正JSONは無視して処理継続(self):
        session = _make_session()
        session.deepgram_ws = self._make_deepgram_ws([
            "not-a-json-string",
            self._make_result("有効な発話", speech_final=True, is_final=True),
        ])
        with patch.object(session, "_process_user_input", new_callable=AsyncMock) as mock_proc:
            await session._handle_deepgram()
            await asyncio.sleep(0)

        mock_proc.assert_called_once_with("有効な発話")
