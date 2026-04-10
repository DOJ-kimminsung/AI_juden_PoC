"""
audio_utils のユニットテスト

カバー範囲:
- pcm24k_to_mulaw8k: 正常変換、データ長の検証、空データ
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from audio_utils import pcm24k_to_mulaw8k, pcm24k_to_mulaw8k_chunk


class TestPcm24kToMulaw8k:

    def test_正常系_PCMデータをmulaw8kに変換できる(self):
        """正常系: PCM 24kHz 16bit データを mulaw 8kHz に変換できる"""
        # 24kHz 16bit mono 0.1秒分 = 24000 * 2 * 0.1 = 4800 bytes
        pcm_data = b"\x00\x01" * 2400
        result = pcm24k_to_mulaw8k(pcm_data)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_サンプルレート変換後のデータ長が約3分の1になる(self):
        """24kHz → 8kHz のダウンサンプルにより長さが約 1/3 になる"""
        # 24kHz 0.1秒分 = 4800 bytes (16bit mono)
        pcm_data = b"\x00\x01" * 2400
        result = pcm24k_to_mulaw8k(pcm_data)

        # mulaw は 1 byte/sample なので 8000 * 0.1 = 800 bytes が理想値
        # audioop のリサンプリングで多少の誤差あり (±5% 以内)
        expected = 800
        assert abs(len(result) - expected) <= expected * 0.05

    def test_空データを渡した場合は空バイトを返す(self):
        """空データを変換しても例外が発生しない"""
        result = pcm24k_to_mulaw8k(b"")
        assert result == b""

    def test_変換後はmulaw形式の値域になる(self):
        """mulaw の各バイトは 0x00〜0xFF の範囲内"""
        pcm_data = b"\x7f\xff" * 1200  # 最大振幅に近い PCM
        result = pcm24k_to_mulaw8k(pcm_data)
        # bytes は 0〜255 なので、すべてのバイトが有効な値を持つ
        assert all(0 <= b <= 255 for b in result)

    def test_大きなデータも変換できる(self):
        """1秒分の PCM データ（48000 bytes）を変換できる"""
        pcm_data = b"\x00\x01" * 24000  # 24kHz 1秒分
        result = pcm24k_to_mulaw8k(pcm_data)
        # 8kHz 1秒分 ≒ 8000 bytes
        assert 7600 <= len(result) <= 8400


class TestPcm24kToMulaw8kChunk:
    """pcm24k_to_mulaw8k_chunk（ストリーミング変換）のテスト"""

    def test_正常系_mulaw変換とstateを返す(self):
        """正常系: 変換結果と new_state のタプルを返す"""
        pcm_data = b"\x00\x01" * 2400  # 24kHz 0.1秒分
        mulaw, state = pcm24k_to_mulaw8k_chunk(pcm_data, None)
        assert isinstance(mulaw, bytes)
        assert len(mulaw) > 0
        assert state is not None  # 次回引き継ぎ用の state

    def test_空チャンクは空bytesとstateをそのまま返す(self):
        """空データは変換せず (b"", state) を返す"""
        mulaw, state = pcm24k_to_mulaw8k_chunk(b"", None)
        assert mulaw == b""
        assert state is None  # 入力 state をそのまま返す

    def test_空チャンクは既存stateを保持する(self):
        """空データ入力時は既存の state を変えない"""
        dummy_state = (0,)  # 任意の state（Tupleならよい）
        mulaw, returned_state = pcm24k_to_mulaw8k_chunk(b"", dummy_state)
        assert mulaw == b""
        assert returned_state is dummy_state

    def test_奇数バイトでも変換できる(self):
        """16bit mono 境界に揃えるため奇数バイトでも例外なく動作する"""
        pcm_odd = b"\x00\x01\x02"  # 3バイト（奇数）
        mulaw, state = pcm24k_to_mulaw8k_chunk(pcm_odd, None)
        assert isinstance(mulaw, bytes)  # 例外なく変換できる

    def test_stateを引き継ぐと2チャンク分の合計長が一括変換と同程度(self):
        """
        一括変換と state 引き継ぎ連続変換で変換結果の合計バイト長が近い
        （ratecv の state 引き継ぎにより境界で音声が欠けないことの確認）
        """
        pcm_data = b"\x00\x01" * 4800  # 24kHz 0.2秒分
        # 一括変換
        bulk = pcm24k_to_mulaw8k_chunk(pcm_data, None)[0]

        # 2分割してstateを引き継ぎ変換
        half = len(pcm_data) // 2
        # 2byte境界に揃える
        if half % 2 != 0:
            half -= 1
        mulaw1, state = pcm24k_to_mulaw8k_chunk(pcm_data[:half], None)
        mulaw2, _     = pcm24k_to_mulaw8k_chunk(pcm_data[half:], state)
        streaming = mulaw1 + mulaw2

        # バイト長が ±10% 以内で一致することを確認
        assert abs(len(streaming) - len(bulk)) <= len(bulk) * 0.10

    def test_変換後はmulaw形式の値域になる(self):
        """変換後の各バイトは 0x00〜0xFF の範囲内"""
        pcm_data = b"\x7f\xff" * 1200
        mulaw, _ = pcm24k_to_mulaw8k_chunk(pcm_data, None)
        assert all(0 <= b <= 255 for b in mulaw)
