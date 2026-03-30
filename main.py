"""
AI受電システム PoC — FastAPI サーバー

エンドポイント:
  POST /incoming-call  ← Twilioのwebhook（着信時）
  WS   /ws/stream      ← Twilioのメディアストリーム
  GET  /health         ← 動作確認用
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()  # call_handler インポート前に呼ぶ（DEEPGRAM_API_KEY等を先に読み込む）

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from openai import AsyncOpenAI

from call_handler import CallSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI受電システム PoC")
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ------------------------------------------------------------------ #
#  Twilio 着信 webhook — TwiML を返してメディアストリームを開始する       #
# ------------------------------------------------------------------ #
@app.post("/incoming-call")
async def incoming_call(request: Request):
    host = request.headers.get("host", "localhost")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/stream"/>
    </Connect>
</Response>"""
    logger.info(f"着信 → Stream開始 host={host}")
    return Response(content=twiml, media_type="application/xml")


# ------------------------------------------------------------------ #
#  Twilio Media Streams WebSocket                                     #
# ------------------------------------------------------------------ #
@app.websocket("/ws/stream")
async def stream_endpoint(websocket: WebSocket):
    await websocket.accept()
    session = CallSession(websocket, openai_client)
    try:
        await session.run()
    except Exception as e:
        logger.error(f"WebSocket エラー: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("WebSocket セッション終了")


# ------------------------------------------------------------------ #
#  ヘルスチェック                                                       #
# ------------------------------------------------------------------ #
@app.get("/health")
async def health():
    return {"status": "ok", "service": "AI受電システム PoC"}
