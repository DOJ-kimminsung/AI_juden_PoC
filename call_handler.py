"""
Twilio Media Streams × Deepgram × GPT-4o × OpenAI TTS
1通話 = 1つの CallSession インスタンス
"""

import asyncio
import base64
import json
import logging
import os
from typing import Optional

import websockets
from fastapi import WebSocket
from openai import AsyncOpenAI

from audio_utils import pcm24k_to_mulaw8k
from scenarios import SYSTEM_PROMPT, GREETING

logger = logging.getLogger(__name__)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?language=ja"
    "&model=nova-2"
    "&encoding=mulaw"
    "&sample_rate=8000"
    "&endpointing=500"       # 500ms の無音で発話終了と判断
    "&interim_results=false" # 確定結果のみ受け取る
)

TTS_MODEL = "tts-1"
TTS_VOICE = "nova"    # 日本語に自然に対応する声
LLM_MODEL = "gpt-4o"
# mulaw 8kHz: 8000 samples/sec × 1 byte/sample × 0.2s = 1600 bytes (PCM換算3200)
# Twilio推奨バッファに合わせて200ms相当のチャンクサイズを使用
AUDIO_CHUNK_SIZE = 3200  # bytes (mulaw, 約200ms相当)

# PERF-001: 会話履歴の最大保持ターン数（超過分は古い順に破棄）
MAX_HISTORY_TURNS = 10


class CallSession:
    def __init__(self, websocket: WebSocket, client: AsyncOpenAI) -> None:
        self.ws = websocket
        self.client = client
        self.stream_sid: Optional[str] = None
        self.deepgram_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.conversation: list[dict[str, str]] = []
        self.is_ai_speaking: bool = False
        self._speak_lock = asyncio.Lock()  # LOGIC-001: レースコンディション防止

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
                # 挨拶を再生
                asyncio.create_task(self._speak(GREETING))

            elif event == "media":
                # AIが話中は音声をDeepgramに転送しない（簡易的な割り込み防止）
                if not self.is_ai_speaking and self.deepgram_ws:
                    audio_bytes = base64.b64decode(data["media"]["payload"])
                    try:
                        await self.deepgram_ws.send(audio_bytes)
                    except websockets.ConnectionClosed:
                        break

            elif event == "stop":
                logger.info("通話終了")
                break

    # ------------------------------------------------------------------ #
    #  Deepgram WebSocket の受信ループ                                      #
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
            is_final = data.get("speech_final", False)

            if is_final and transcript:
                logger.info(f"ユーザー発話: {transcript}")
                # 非同期でAI応答を生成・送信（Twilioループをブロックしない）
                asyncio.create_task(self._process_user_input(transcript))

    # ------------------------------------------------------------------ #
    #  GPT-4o で応答を生成し、TTSで返す                                     #
    # ------------------------------------------------------------------ #
    async def _process_user_input(self, text: str) -> None:
        self.conversation.append({"role": "user", "content": text})

        # PERF-001: 会話履歴の上限管理（古いターンを破棄してトークン超過を防止）
        if len(self.conversation) > MAX_HISTORY_TURNS * 2:
            self.conversation = self.conversation[-(MAX_HISTORY_TURNS * 2):]
            logger.info(f"会話履歴を直近{MAX_HISTORY_TURNS}ターンに切り詰めました")

        try:
            response = await self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *self.conversation,
                ],
                max_tokens=200,
                temperature=0.7,
            )
            ai_text = response.choices[0].message.content
            self.conversation.append({"role": "assistant", "content": ai_text})
            logger.info(f"AI応答: {ai_text}")
            await self._speak(ai_text)

        except Exception as e:
            logger.error(f"LLM エラー: {e}")

    # ------------------------------------------------------------------ #
    #  OpenAI TTS → mulaw変換 → Twilioへ送信                               #
    # ------------------------------------------------------------------ #
    async def _speak(self, text: str) -> None:
        if not self.stream_sid:
            return

        # LOGIC-001: asyncio.Lock で排他制御（複数タスクの同時実行を防止）
        # Lock を保持したまま TTS 送信全体を実行し、フラグの競合を排除する
        async with self._speak_lock:
            self.is_ai_speaking = True
            try:
                tts_response = await self.client.audio.speech.create(
                    model=TTS_MODEL,
                    voice=TTS_VOICE,
                    input=text,
                    response_format="pcm",  # raw PCM 24kHz 16bit mono
                )
                pcm_data = tts_response.content
                mulaw_data = pcm24k_to_mulaw8k(pcm_data)

                # チャンク送信（Twilioのバッファに合わせる）
                for i in range(0, len(mulaw_data), AUDIO_CHUNK_SIZE):
                    chunk = mulaw_data[i : i + AUDIO_CHUNK_SIZE]
                    payload = base64.b64encode(chunk).decode("utf-8")
                    await self.ws.send_json(
                        {
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {"payload": payload},
                        }
                    )
                    await asyncio.sleep(0)  # 他タスクに制御を返す

                # 音声送信完了をTwilioに通知
                await self.ws.send_json(
                    {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": "audio_end"},
                    }
                )

            except Exception as e:
                logger.error(f"TTS エラー: {e}")
            finally:
                self.is_ai_speaking = False

    # ------------------------------------------------------------------ #
    #  Twilioの再生キューをクリア（割り込み対応用）                          #
    # ------------------------------------------------------------------ #
    async def _clear_audio(self):
        if self.stream_sid:
            await self.ws.send_json(
                {"event": "clear", "streamSid": self.stream_sid}
            )
