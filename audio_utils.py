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
    Twilio 用の mulaw (8kHz) に変換する（一括変換）
    """
    # 24kHz → 8kHz にダウンサンプリング
    resampled, _ = audioop.ratecv(pcm_data, 2, 1, 24000, 8000, None)
    # linear PCM → mulaw
    mulaw_data = audioop.lin2ulaw(resampled, 2)
    return mulaw_data


def pcm24k_to_mulaw8k_chunk(pcm_chunk: bytes, state=None):
    """
    ストリーミング変換: audioop.ratecv の state を引き継ぎ、
    チャンク境界での音声乱れ（クリック音・位相ずれ）を防ぐ。

    Args:
        pcm_chunk: PCM 24kHz 16bit mono のチャンク（任意バイト数）
        state:     前回の ratecv 戻り値の第2要素（初回 None）

    Returns:
        tuple[bytes, object]: (mulaw_bytes, new_state)
            mulaw_bytes: 変換済み mulaw 8kHz データ
            new_state:   次回呼び出しに渡す state

    Notes:
        - pcm_chunk が空の場合は (b"", state) をそのまま返す
        - 奇数バイトが来た場合は末尾1バイトをトリムして変換する
          （16bit = 2byte/sample の境界を保つため）
    """
    if not pcm_chunk:
        return b"", state

    # 2byte 境界（16bit mono）に揃える
    if len(pcm_chunk) % 2 != 0:
        pcm_chunk = pcm_chunk[:-1]

    resampled, new_state = audioop.ratecv(pcm_chunk, 2, 1, 24000, 8000, state)
    mulaw_data = audioop.lin2ulaw(resampled, 2)
    return mulaw_data, new_state
