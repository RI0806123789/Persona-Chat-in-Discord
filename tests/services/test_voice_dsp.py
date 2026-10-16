"""音色プロファイルの数値の検証と、bit_crush の判定と適用の一致を確かめる（#67）。"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.audio import voice_dsp

SAMPLE_RATE = 48000


class EqualizerValueTests(unittest.TestCase):
    @staticmethod
    def equalizer(**settings):
        # 実際の音色表は変えず、架空の音色名だけを一時的に足して判定する
        return patch.dict(voice_dsp.VOICE_EQ_PROFILES, {"fictional": settings})

    def test_non_finite_values_fall_back_to_default(self) -> None:
        for value in ("nan", "inf", "-inf", float("nan"), float("inf"), None, "架空"):
            with self.subTest(value=value):
                self.assertEqual(voice_dsp._as_float(value, 1.5), 1.5)
        self.assertEqual(voice_dsp._as_float("2.5", 0.0), 2.5)
        self.assertEqual(voice_dsp._as_float(-3, 0.0), -3.0)

    def test_bit_crush_decision_matches_what_is_applied(self) -> None:
        samples = np.linspace(-1000.0, 1000.0, 64)
        cases = [
            ("0", False), (0, False), (-1, False), (0.5, False), ("nan", False), ("inf", False),
            (None, False), ("架空", False), (1, True), ("6", True), (6.9, True),
        ]
        for value, crushed in cases:
            with self.subTest(bit_crush=value), self.equalizer(bit_crush=value):
                profile = {"eq": "fictional"}
                self.assertEqual(voice_dsp.needs_equalizer(profile), crushed)
                result = voice_dsp.apply_equalizer(samples, profile, sample_rate=SAMPLE_RATE)
                self.assertTrue(np.all(np.isfinite(result)))
                self.assertEqual(not np.array_equal(result, samples), crushed)

    def test_non_finite_filter_settings_do_not_produce_nan_audio(self) -> None:
        samples = np.sin(np.linspace(0.0, 20.0, 480)) * 1000.0
        base = {"kind": "high_shelf", "frequency": 3000.0, "gain_db": 6.0, "q": 0.7}
        for field in ("frequency", "gain_db", "q"):
            for value in ("nan", "inf", float("nan")):
                with self.subTest(field=field, value=value), self.equalizer(**{**base, field: value}):
                    result = voice_dsp.apply_equalizer(samples, {"eq": "fictional"}, sample_rate=SAMPLE_RATE)
                    self.assertTrue(np.all(np.isfinite(result)))

    def test_bundled_profiles_keep_their_decision(self) -> None:
        # 実際の音色表の判定が変わっていないこと（加工なしの flat と、bit_crush を持つ robotic）
        self.assertFalse(voice_dsp.needs_equalizer({"eq": "flat"}))
        self.assertTrue(voice_dsp.needs_equalizer({"eq": "robotic"}))


if __name__ == "__main__":
    unittest.main()
