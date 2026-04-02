"""
AI受電システム PoC — FastAPI サーバー

エンドポイント:
  POST /incoming-call  ← Twilioのwebhook（着信時）
  WS   /ws/stream      ← Twilioのメディアストリーム
  GET  /health         ← 動作確認用
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # call_handler インポート前に呼ぶ（DEEPGRAM_API_KEY等を先に読み込む）

from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import Response
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator

from call_handler import CallSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  起動時の環境変数バリデーション（SEC-004）                              #
# ------------------------------------------------------------------ #
_REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "PUBLIC_HOST",
]
for _var in _REQUIRED_ENV_VARS:
    if not os.getenv(_var):
        raise EnvironmentError(f"必須環境変数が未設定です: {_var}")

PUBLIC_HOST: str = os.environ["PUBLIC_HOST"]
TWILIO_AUTH_TOKEN: str = os.environ["TWILIO_AUTH_TOKEN"]

# 同時接続数の上限（PERF-002）
MAX_CONCURRENT_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "10"))
_call_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

app = FastAPI(title="AI受電システム PoC")
openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)


# ------------------------------------------------------------------ #
#  Twilio 着信 webhook — TwiML を返してメディアストリームを開始する       #
# ------------------------------------------------------------------ #
@app.post("/incoming-call")
async def incoming_call(request: Request):
    # SEC-001: Twilio署名検証 (X-Twilio-Signature)
    form_data = await request.form()
    url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not twilio_validator.validate(url, dict(form_data), signature):
        logger.warning(f"Twilio署名検証失敗 url={url}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    # SEC-002: ホスト名は環境変数で固定（ヘッダーインジェクション防止）
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{PUBLIC_HOST}/ws/stream"/>
    </Connect>
</Response>"""
    logger.info(f"着信 → Stream開始 host={PUBLIC_HOST}")
    return Response(content=twiml, media_type="application/xml")


# ------------------------------------------------------------------ #
#  Twilio Media Streams WebSocket                                     #
# ------------------------------------------------------------------ #
@app.websocket("/ws/stream")
async def stream_endpoint(websocket: WebSocket):
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info(f"WebSocket接続試行 from={client_host}")
    await websocket.accept()
    logger.info(f"WebSocket接続確立 from={client_host}")

    # PERF-002: 同時接続数の制限
    if _call_semaphore.locked() and _call_semaphore._value == 0:
        logger.warning(f"同時接続上限({MAX_CONCURRENT_CALLS})に達しました")
        await websocket.close(code=1013)  # 1013: Try Again Later
        return

    async with _call_semaphore:
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
