"""
音声フォーマット変換ユーティリティ
Twilio  : mulaw 8kHz
Deepgram : mulaw 8kHz (そのまま転送)
OpenAI TTS: PCM 24kHz 16bit mono → mulaw 8kHz に変換してTwilioへ返す
"""

try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop  # Python 3.13+
    except ImportError:
        raise ImportError("audioop が見つかりません。Python 3.13以上の場合: pip install audioop-lts")


def pcm24k_to_mulaw8k(pcm_data: bytes) -> bytes:
    """
    OpenAI TTS の PCM出力 (24kHz, 16bit, mono) を
    Twilio 用の mulaw (8kHz) に変換する
    """
    # 24kHz → 8kHz にダウンサンプリング
    resampled, _ = audioop.ratecv(pcm_data, 2, 1, 24000, 8000, None)
    # linear PCM → mulaw
    mulaw_data = audioop.lin2ulaw(resampled, 2)
    return mulaw_data
