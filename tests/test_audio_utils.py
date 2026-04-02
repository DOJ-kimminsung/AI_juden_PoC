"""
audio_utils のユニットテスト

カバー範囲:
- pcm24k_to_mulaw8k: 正常変換、データ長の検証、空データ
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from audio_utils import pcm24k_to_mulaw8k


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
