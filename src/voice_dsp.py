"""読み上げ音声に音色（EQ）を掛ける軽量 DSP（numpy のみ）。

キャラクターごとの「声の高さ」と「話す速さ」は edge-tts のサーバー側パラメータ
（`pitch` / `rate`）が担当するため、このモジュールが受け持つのは**音色だけ**。
edge-tts には音色を変える機能が無く、同じ話者を使い回す9キャラの差を付けるには
こちらで作る必要がある。

CPU 1コア / メモリ 1GB 級のサーバーで動かす前提のため、`scipy` や `librosa` の
ような重量級ライブラリは使わず numpy だけで完結させている。

なお設計初期には、gTTS の単一音声から高さと速さを作り出すために WSOLA
（時間伸縮）とリサンプリング（ピッチ変更）もここで行っていた。edge-tts への
移行でサーバー側が同じことを、しかもフォルマントを保ったまま行えるように
なったため削除した（詳細は VOICE_PROFILE_DESIGN.md の 4〜5 章）。
"""

import math
from typing import Any, Mapping

import numpy as np

from constants import (
    VOICE_EQ_PROFILES,
    VOICE_PROFILE_FALLBACK,
    VOICE_PROFILES,
)

# バイクアッドフィルタを一度に処理するサンプル数。
# IIR フィルタは1つ前の出力を使う再帰計算のため numpy でベクトル化できず、
# Python のループで回すしかない。全サンプルを一気に list 化すると 30 秒の音声で
# 数十 MB の一時オブジェクトができてしまうため、メモリ 1GB の環境を想定して
# 固定長のブロックに区切る（フィルタの状態は次のブロックへ持ち越すので結果は同じ）。
_BIQUAD_BLOCK_SIZE = 65536

# 0 除算を避けるための微小値。
_EPSILON = 1e-9


def resolve_profile(prompt_file: str | None) -> Mapping[str, Any]:
    """ペルソナのファイルパスから、そのキャラクターのボイスプロファイルを引く。

    未登録のペルソナ（今後 prompts/ に追加されたもの）は既定値に落とす。
    """
    if not prompt_file:
        return VOICE_PROFILE_FALLBACK
    return VOICE_PROFILES.get(prompt_file, VOICE_PROFILE_FALLBACK)


def needs_equalizer(profile: Mapping[str, Any]) -> bool:
    """このプロファイルが音色の加工を必要とするかどうか。

    不要と分かっていれば、呼び出し側は miniaudio に 48kHz ステレオまで
    一気に変換させて numpy の処理そのものを飛ばせる。
    """
    equalizer = _resolve_equalizer(profile)
    return bool(equalizer.get("kind")) or bool(equalizer.get("bit_crush"))


def apply_equalizer(
    samples: np.ndarray,
    profile: Mapping[str, Any],
    *,
    sample_rate: int,
) -> np.ndarray:
    """モノラル PCM にプロファイルの音色を適用する。

    戻り値は float64 のまま返す（int16 への丸めとクリップは呼び出し側で行う）。
    """
    audio = np.asarray(samples, dtype=np.float64)
    if audio.size == 0:
        return audio

    equalizer = _resolve_equalizer(profile)
    kind = equalizer.get("kind")

    if kind:
        coefficients = _biquad_coefficients(
            str(kind),
            # 折り返し（エイリアシング）を避けるためナイキスト周波数の手前に抑える。
            _clamp(_as_float(equalizer.get("frequency"), 1000.0), 10.0, sample_rate * 0.45),
            _as_float(equalizer.get("gain_db"), 0.0),
            max(_as_float(equalizer.get("q"), 0.707), 0.1),
            sample_rate,
        )
        if coefficients is not None:
            audio = _apply_biquad(audio, coefficients)

    bits = int(_as_float(equalizer.get("bit_crush"), 0.0))
    if bits > 0:
        audio = _bit_crush(audio, bits)

    return audio


# --- 内部ヘルパー -----------------------------------------------------------


def _as_float(value: Any, default: float) -> float:
    """プロファイルの値を float にする。数値でなければ既定値へ落とす。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _resolve_equalizer(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """プロファイルの `eq` 名から EQ の設定を引く（未知の名前は加工なし）。"""
    name = profile.get("eq") or "flat"
    return VOICE_EQ_PROFILES.get(str(name), VOICE_EQ_PROFILES["flat"])


def _biquad_coefficients(
    kind: str,
    frequency: float,
    gain_db: float,
    q: float,
    sample_rate: int,
) -> tuple[float, float, float, float, float] | None:
    """Audio EQ Cookbook（Robert Bristow-Johnson）の公式でフィルタ係数を作る。

    `scipy.signal.butter()` 等を使えば1行だが、scipy はインストール容量と
    常駐メモリを増やすため、公式どおりの計算を自前で持つ。
    戻り値は a0 で正規化済みの `(b0, b1, b2, a1, a2)`。
    """
    omega = 2.0 * math.pi * frequency / sample_rate
    cosine = math.cos(omega)
    alpha = math.sin(omega) / (2.0 * q)

    if kind == "low_pass":
        b0 = (1.0 - cosine) / 2.0
        b1 = 1.0 - cosine
        b2 = (1.0 - cosine) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cosine
        a2 = 1.0 - alpha
    elif kind == "notch":
        b0 = 1.0
        b1 = -2.0 * cosine
        b2 = 1.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cosine
        a2 = 1.0 - alpha
    elif kind == "high_shelf":
        # A は「持ち上げ量の平方根」。dB をゲイン倍率へ直すときに 40 で割るのは、
        # シェルフの公式が A の2乗で効くように組まれているため。
        # gain_db がマイナスなら A < 1 となり、そのまま減衰として働く。
        amplitude = 10.0 ** (gain_db / 40.0)
        sqrt_a = math.sqrt(amplitude)
        b0 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cosine + 2.0 * sqrt_a * alpha)
        b1 = -2.0 * amplitude * ((amplitude - 1.0) + (amplitude + 1.0) * cosine)
        b2 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cosine - 2.0 * sqrt_a * alpha)
        a0 = (amplitude + 1.0) - (amplitude - 1.0) * cosine + 2.0 * sqrt_a * alpha
        a1 = 2.0 * ((amplitude - 1.0) - (amplitude + 1.0) * cosine)
        a2 = (amplitude + 1.0) - (amplitude - 1.0) * cosine - 2.0 * sqrt_a * alpha
    else:
        # 未知の種別。加工しない方が、想定外の係数で音を壊すより安全。
        return None

    if abs(a0) < _EPSILON:
        return None
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def _apply_biquad(
    samples: np.ndarray,
    coefficients: tuple[float, float, float, float, float],
) -> np.ndarray:
    """2次 IIR フィルタ（直接形 I）を適用する。

        y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] - a1*y[n-1] - a2*y[n-2]

    1つ前の「出力」を使う再帰式のため numpy のベクトル演算では書けず、
    Python のループで1サンプルずつ回す。一時メモリを抑えるためブロックに
    区切って処理するが、フィルタの状態（x1/x2/y1/y2）を持ち越すので
    一気に処理した場合と結果は完全に同じ。
    """
    b0, b1, b2, a1, a2 = coefficients
    output = np.empty_like(samples)
    x1 = x2 = y1 = y2 = 0.0

    for start in range(0, samples.size, _BIQUAD_BLOCK_SIZE):
        # numpy スカラーのボックス化を避けるため list 化してから回す（数倍速い）。
        block = samples[start:start + _BIQUAD_BLOCK_SIZE].tolist()
        filtered = [0.0] * len(block)
        for index, x0 in enumerate(block):
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            filtered[index] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0
        output[start:start + len(block)] = filtered

    return output


def _bit_crush(samples: np.ndarray, bits: int) -> np.ndarray:
    """量子化を粗くして、デジタルらしいザラついた響きを足す。

    int16（16ビット）の刻み幅を 2^(16-bits) 倍に広げ、その格子に丸める。
    bits が小さいほど段差が大きくなり、機械的な質感が強くなる。
    """
    step = 2.0 ** (16 - _clamp(float(bits), 1.0, 16.0))
    return np.round(samples / step) * step
