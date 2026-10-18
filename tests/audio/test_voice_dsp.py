"""プロファイルの退避と、フィルタの周波数特性・量子化の境界を確認する。"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.audio import voice_dsp


class VoiceDspTests(unittest.TestCase):
    def test_registered_and_missing_personas_resolve_to_expected_profile(self):
        selected, fallback = {"voice": "架空の話者"}, {"voice": "架空の既定話者"}
        with patch.object(voice_dsp, "VOICE_PROFILES", {"fictional.txt": selected}), patch.object(
            voice_dsp, "VOICE_PROFILE_FALLBACK", fallback,
        ):
            self.assertIs(voice_dsp.resolve_profile("fictional.txt"), selected)
            for prompt in (None, "", "unknown.txt"):
                self.assertIs(voice_dsp.resolve_profile(prompt), fallback)

    def test_empty_audio_is_safe_for_every_filter_kind(self):
        for kind in ("low_pass", "notch", "high_shelf", "unknown"):
            with self.subTest(kind=kind), patch.dict(voice_dsp.VOICE_EQ_PROFILES, {
                "fictional": {"kind": kind, "bit_crush": 6},
            }):
                result = voice_dsp.apply_equalizer(np.array([], dtype=np.int16), {"eq": "fictional"}, sample_rate=48000)
                self.assertEqual(result.size, 0)
                self.assertEqual(result.dtype, np.float64)

    def test_low_pass_preserves_dc_and_attenuates_high_frequency(self):
        coefficients = voice_dsp._biquad_coefficients("low_pass", 1000.0, 0, 0.707, 48000)
        dc = voice_dsp._apply_biquad(np.ones(4096), coefficients)
        high = np.sin(2 * np.pi * 10000 * np.arange(4096) / 48000)
        filtered = voice_dsp._apply_biquad(high, coefficients)
        self.assertAlmostEqual(float(dc[-1]), 1.0, places=8)
        self.assertLess(float(np.sqrt(np.mean(filtered[1024:] ** 2))), 0.02)

    def test_notch_removes_its_center_frequency(self):
        coefficients = voice_dsp._biquad_coefficients("notch", 3000.0, 0, 0.707, 48000)
        signal = np.sin(2 * np.pi * 3000 * np.arange(4096) / 48000)
        result = voice_dsp._apply_biquad(signal, coefficients)
        self.assertLess(float(np.max(np.abs(result[1024:]))), 1e-8)

    def test_zero_gain_shelf_and_unknown_filter_preserve_samples(self):
        samples = np.linspace(-1000, 1000, 101)
        for kind in ("high_shelf", "unknown"):
            with self.subTest(kind=kind), patch.dict(voice_dsp.VOICE_EQ_PROFILES, {
                "fictional": {"kind": kind, "frequency": 3000.0, "gain_db": 0.0, "q": 0.7},
            }):
                result = voice_dsp.apply_equalizer(samples, {"eq": "fictional"}, sample_rate=48000)
                np.testing.assert_allclose(result, samples, rtol=0, atol=1e-9)

    def test_frequency_extremes_stay_finite_and_do_not_mutate_input(self):
        samples = np.linspace(-1000, 1000, 128)
        original = samples.copy()
        for frequency in (-1e6, 1e6):
            for kind in ("low_pass", "notch", "high_shelf"):
                with self.subTest(frequency=frequency, kind=kind), patch.dict(voice_dsp.VOICE_EQ_PROFILES, {
                    "fictional": {"kind": kind, "frequency": frequency, "gain_db": 6.0, "q": 0.7},
                }):
                    result = voice_dsp.apply_equalizer(samples, {"eq": "fictional"}, sample_rate=48000)
                    self.assertTrue(np.all(np.isfinite(result)))
                    self.assertEqual(result.shape, samples.shape)
                    np.testing.assert_array_equal(samples, original)

    def test_bit_crush_quantizes_samples_and_clamps_bit_depth(self):
        samples = np.array([-900.0, -100.0, 0.0, 100.0, 900.0])
        np.testing.assert_array_equal(voice_dsp._bit_crush(samples, 8), [-1024, 0, 0, 0, 1024])
        np.testing.assert_array_equal(voice_dsp._bit_crush(samples, 100), samples)
        np.testing.assert_array_equal(voice_dsp._bit_crush(samples, 0), voice_dsp._bit_crush(samples, 1))


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
