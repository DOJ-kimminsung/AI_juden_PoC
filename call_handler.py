"""
Twilio Media Streams × Deepgram × GPT-4o × OpenAI TTS
1通話 = 1つの CallSession インスタンス

変更点:
  - バージイン機能: Deepgram の interim_results を利用し、AI 発話中の割り込みを検出
  - ストリーミング: LLM・TTS 両方をストリーミング化し、センテンス単位で逐次送信
  - 応答遅延の大幅削減（目標: 1秒以内）
"""

import asyncio
import base64
import json
import logging
import os
import re
from typing import Optional

import websockets
from fastapi import WebSocket
from openai import AsyncOpenAI

from audio_utils import pcm24k_to_mulaw8k_chunk
from scenarios import SYSTEM_PROMPT, GREETING

logger = logging.getLogger(__name__)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?language=ja"
    "&model=nova-2"
    "&encoding=mulaw"
    "&sample_rate=8000"
    "&endpointing=300"        # 500ms → 300ms（応答速度向上）
    "&interim_results=true"   # バージイン検出のため中間結果を受け取る
)

TTS_MODEL = "tts-1"
TTS_VOICE = "nova"    # 日本語に自然に対応する声
LLM_MODEL = "gpt-4o"
# mulaw 8kHz: 8000 samples/sec × 1 byte/sample × 0.2s = 1600 bytes
AUDIO_CHUNK_SIZE = 3200  # bytes (mulaw, 約200ms相当)

# PERF-001: 会話履歴の最大保持ターン数
MAX_HISTORY_TURNS = 10

# センテンス分割: 文末文字で区切り、最初のセンテンスから順次 TTS に渡す
_SENTENCE_END_RE = re.compile(r"(?<=[。！？\n])")
_MIN_SENTENCE_LEN = 10  # これ未満は次の区切りまでバッファリング


class CallSession:
    def __init__(self, websocket: WebSocket, client: AsyncOpenAI) -> None:
        self.ws = websocket
        self.client = client
        self.stream_sid: Optional[str] = None
        self.deepgram_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.conversation: list[dict[str, str]] = []
        self.is_ai_speaking: bool = False
        # バージイン・タスク管理
        self._speak_task: Optional[asyncio.Task] = None    # 現在の発話タスク
        self._process_task: Optional[asyncio.Task] = None  # 現在の応答生成タスク
        self._barge_in_triggered: bool = False             # 二重割り込み防止

    # ------------------------------------------------------------------ #
    #  エントリーポイント                                                   #
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            async with websockets.connect(
                DEEPGRAM_URL, extra_headers=headers
            ) as dg_ws:
                self.deepgram_ws = dg_ws
                logger.info("Deepgram 接続完了")
                # LOGIC-003: タスクを明示管理し、一方が終了したら他方もキャンセル
                tasks = [
                    asyncio.create_task(self._handle_twilio()),
                    asyncio.create_task(self._handle_deepgram()),
                ]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"CallSession エラー: {e}")

    # ------------------------------------------------------------------ #
    #  Twilio WebSocket の受信ループ                                        #
    # ------------------------------------------------------------------ #
    async def _handle_twilio(self):
        async for raw in self.ws.iter_text():
            data = json.loads(raw)
            event = data.get("event")

            if event == "start":
                self.stream_sid = data["start"]["streamSid"]
                logger.info(f"通話開始 streamSid={self.stream_sid}")
                # 挨拶を再生（ストリーミング TTS）
                self._speak_task = asyncio.create_task(self._speak_greeting())

            elif event == "media":
                # バージイン対応: is_ai_speaking に関わらず常に Deepgram へ転送
                # （発話中でも音声認識を継続させてバージインを検出する）
                if self.deepgram_ws:
                    audio_bytes = base64.b64decode(data["media"]["payload"])
                    try:
                        await self.deepgram_ws.send(audio_bytes)
                    except websockets.ConnectionClosed:
                        break

            elif event == "stop":
                logger.info("通話終了")
                break

    # ------------------------------------------------------------------ #
    #  Deepgram WebSocket の受信ループ（バージイン検出付き）                 #
    # ------------------------------------------------------------------ #
    async def _handle_deepgram(self):
        async for raw in self.deepgram_ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") != "Results":
                continue

            alternatives = data.get("channel", {}).get("alternatives", [])
            if not alternatives:
                continue

            transcript = alternatives[0].get("transcript", "").strip()
            is_final = data.get("is_final", False)       # utterance chunk の確定
            speech_final = data.get("speech_final", False)  # 発話終了（endpointing）

            # ── バージイン検出: 中間結果 かつ AI 発話中 ──
            if (
                not is_final
                and transcript
                and self.is_ai_speaking
                and not self._barge_in_triggered
            ):
                logger.info(f"バージイン検出: '{transcript}'")
                self._barge_in_triggered = True
                asyncio.create_task(self._cancel_current_speak())

            # ── 確定発話: AI 応答を生成 ──
            if speech_final and transcript:
                logger.info(f"ユーザー発話(確定): {transcript}")
                self._barge_in_triggered = False
                # 前の処理タスクをキャンセルして新しい処理を開始
                if self._process_task and not self._process_task.done():
                    self._process_task.cancel()
                self._process_task = asyncio.create_task(
                    self._process_user_input(transcript)
                )

    # ------------------------------------------------------------------ #
    #  バージイン: 現在の発話をキャンセルし Twilio キューをクリア            #
    # ------------------------------------------------------------------ #
    async def _cancel_current_speak(self) -> None:
        """現在の発話タスクをキャンセルし、Twilio の再生キューをクリアする"""
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._speak_task, return_exceptions=True),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                logger.warning("speak タスクのキャンセル待ちがタイムアウト")
        await self._clear_audio()
        logger.info("AI 発話を中断し Twilio キューをクリアしました")

    # ------------------------------------------------------------------ #
    #  GPT-4o ストリーミング応答を生成・送信                                 #
    # ------------------------------------------------------------------ #
    async def _process_user_input(self, text: str) -> None:
        self.conversation.append({"role": "user", "content": text})

        # PERF-001: 会話履歴の上限管理（古いターンを破棄してトークン超過を防止）
        if len(self.conversation) > MAX_HISTORY_TURNS * 2:
            self.conversation = self.conversation[-(MAX_HISTORY_TURNS * 2):]
            logger.info(f"会話履歴を直近{MAX_HISTORY_TURNS}ターンに切り詰めました")

        full_response = ""
        try:
            stream = await self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *self.conversation,
                ],
                max_tokens=200,
                temperature=0.7,
                stream=True,
            )

            # _speak_streaming をタスクとして起動・追跡
            self._speak_task = asyncio.create_task(
                self._speak_streaming(stream)
            )
            full_response = await self._speak_task

        except asyncio.CancelledError:
            logger.info("処理タスクがキャンセルされました")
        except Exception as e:
            logger.error(f"LLM エラー: {e}")
        finally:
            if full_response:
                self.conversation.append({"role": "assistant", "content": full_response})

    # ------------------------------------------------------------------ #
    #  LLM ストリーム → センテンス単位 TTS 送信                             #
    # ------------------------------------------------------------------ #
    async def _speak_streaming(self, stream) -> str:
        """
        LLM ストリームを受け取り、センテンス単位で TTS ストリーミング送信する。
        Returns: 全文テキスト（会話履歴追加用）
        """
        if not self.stream_sid:
            return ""

        self.is_ai_speaking = True
        full_text = ""
        buf = ""

        try:
            async for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                buf += delta
                full_text += delta

                # 文末文字で分割してセンテンスを順次 TTS へ
                parts = _SENTENCE_END_RE.split(buf)
                # 最後の要素は未完成の可能性があるので残す
                for sentence in parts[:-1]:
                    sentence = sentence.strip()
                    if len(sentence) >= _MIN_SENTENCE_LEN:
                        await self._tts_stream_send(sentence)
                buf = parts[-1]

            # LLM 終了後の残りバッファを処理
            if buf.strip():
                await self._tts_stream_send(buf.strip())

            logger.info(f"AI 応答(全文): {full_text}")
            return full_text

        except asyncio.CancelledError:
            logger.info("_speak_streaming がキャンセルされました")
            return full_text
        except Exception as e:
            logger.error(f"ストリーミング発話エラー: {e}")
            return full_text
        finally:
            self.is_ai_speaking = False

    # ------------------------------------------------------------------ #
    #  挨拶専用: 単発テキストをストリーミング TTS で送信                     #
    # ------------------------------------------------------------------ #
    async def _speak_greeting(self) -> None:
        self.is_ai_speaking = True
        try:
            await self._tts_stream_send(GREETING)
        except asyncio.CancelledError:
            pass
        finally:
            self.is_ai_speaking = False

    # ------------------------------------------------------------------ #
    #  1センテンスを TTS ストリーミングで Twilio へ送信                      #
    # ------------------------------------------------------------------ #
    async def _tts_stream_send(self, text: str) -> None:
        if not self.stream_sid or not text:
            return
        logger.debug(f"TTS送信: {text}")

        try:
            async with self.client.audio.speech.with_streaming_response.create(
                model=TTS_MODEL,
                voice=TTS_VOICE,
                input=text,
                response_format="pcm",  # raw PCM 24kHz 16bit mono
            ) as response:
                ratecv_state = None
                async for pcm_chunk in response.aiter_bytes(chunk_size=4096):
                    mulaw_chunk, ratecv_state = pcm24k_to_mulaw8k_chunk(
                        pcm_chunk, ratecv_state
                    )
                    if mulaw_chunk:
                        await self._send_audio_chunks(mulaw_chunk)

            # センテンス末尾の mark イベント
            await self.ws.send_json(
                {
                    "event": "mark",
                    "streamSid": self.stream_sid,
                    "mark": {"name": "sentence_end"},
                }
            )

        except asyncio.CancelledError:
            raise  # 上位に伝播させる
        except Exception as e:
            logger.error(f"TTS ストリームエラー: {e}")

    # ------------------------------------------------------------------ #
    #  mulaw データをチャンク分割して Twilio へ送信                          #
    # ------------------------------------------------------------------ #
    async def _send_audio_chunks(self, mulaw_data: bytes) -> None:
        for i in range(0, len(mulaw_data), AUDIO_CHUNK_SIZE):
            chunk = mulaw_data[i: i + AUDIO_CHUNK_SIZE]
            payload = base64.b64encode(chunk).decode("utf-8")
            await self.ws.send_json(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": payload},
                }
            )
            await asyncio.sleep(0)  # 他タスクに制御を返す

    # ------------------------------------------------------------------ #
    #  Twilioの再生キューをクリア（バージイン時に使用）                      #
    # ------------------------------------------------------------------ #
    async def _clear_audio(self):
        if self.stream_sid:
            await self.ws.send_json(
                {"event": "clear", "streamSid": self.stream_sid}
            )
