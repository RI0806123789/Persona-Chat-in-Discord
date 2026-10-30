"""環境ごとの pip freeze から constraints.txt を作る合成スクリプト（Issue #285）を検証する。"""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import merge_constraints as mc

try:
    # 出力した環境マーカーの文字列を、pip と同じ解釈で評価して確かめる（matplotlib が引き込む packaging）
    from packaging.markers import Marker
except ImportError:  # pragma: no cover - 依存の構成が変わった環境
    Marker = None

LINUX = "linux"
WINDOWS = "win32"
PYTHONS = ((3, 11), (3, 12), (3, 13))
ALL_ENVS = [mc.Environment(python, platform) for platform in (LINUX, WINDOWS) for python in PYTHONS]


def _freezes(**overrides: dict[mc.Environment, str]) -> dict[mc.Environment, dict[str, str]]:
    """全環境に共通の依存へ、環境ごとの版（名前 → {環境: 版}）を重ねた pip freeze の組。"""
    freezes = {env: {"pip": "26.2.1", "setuptools": "79.0.1", "aiohttp": "3.14.4"} for env in ALL_ENVS}
    for name, versions in overrides.items():
        for env, version in versions.items():
            freezes[env][name] = version
    return freezes


def _env(platform: str, python: tuple[int, int]) -> mc.Environment:
    return mc.Environment(python, platform)


class MergeTests(unittest.TestCase):
    def assert_markers_select_one_line(self, lines: list[str], freezes: dict) -> None:
        """どの環境でも、依存ごとにちょうど1行だけが当てはまり、その版が pip freeze と一致する。"""
        if Marker is None:
            self.skipTest("packaging が無いため環境マーカーを評価できません")
        for env, pins in freezes.items():
            environment = {"python_version": env.python_text, "sys_platform": env.platform}
            chosen: dict[str, list[str]] = {}
            for line in lines:
                requirement, _, marker = line.partition(";")
                name, version = requirement.strip().split("==")
                if not marker or Marker(marker.strip()).evaluate(environment):
                    chosen.setdefault(name, []).append(version)
            for name, versions in chosen.items():
                with self.subTest(env=env, name=name):
                    if name in pins:
                        self.assertEqual(versions, [pins[name]])
                    else:
                        self.assertLessEqual(len(versions), 1)  # 入らない依存の制約は使われない

    def test_same_version_everywhere_needs_no_marker(self) -> None:
        # colorama のように一部の環境にしか入らない依存も、版が1つなら条件なしでよい
        freezes = _freezes(colorama={_env(WINDOWS, python): "0.4.6" for python in PYTHONS})
        lines = mc.merge_constraints(freezes, direct=[])
        self.assertEqual(lines, ["aiohttp==3.14.4", "colorama==0.4.6"])
        self.assert_markers_select_one_line(lines, freezes)

    def test_version_split_by_python_uses_ranges(self) -> None:
        # contourpy 1.4.0 は Python 3.12 以上が必須で、3.11 は 1.3.3 になる
        freezes = _freezes(
            contourpy={env: ("1.3.3" if env.python == (3, 11) else "1.4.0") for env in ALL_ENVS}
        )
        lines = mc.merge_constraints(freezes, direct=[])
        self.assertIn('contourpy==1.3.3; python_version < "3.12"', lines)
        self.assertIn('contourpy==1.4.0; python_version >= "3.12"', lines)
        self.assert_markers_select_one_line(lines, freezes)

    def test_middle_and_non_contiguous_python_versions(self) -> None:
        freezes = _freezes(
            middle={env: ("2.0" if env.python == (3, 12) else "1.0") for env in ALL_ENVS},
        )
        lines = mc.merge_constraints(freezes, direct=[])
        self.assertIn('middle==2.0; python_version >= "3.12" and python_version < "3.13"', lines)
        self.assertIn('middle==1.0; (python_version == "3.11" or python_version == "3.13")', lines)
        self.assert_markers_select_one_line(lines, freezes)

    def test_version_split_by_platform(self) -> None:
        freezes = _freezes(tool={env: ("1.0" if env.platform == WINDOWS else "1.1") for env in ALL_ENVS})
        lines = mc.merge_constraints(freezes, direct=[])
        self.assertIn('tool==1.1; sys_platform == "linux"', lines)
        self.assertIn('tool==1.0; sys_platform == "win32"', lines)
        self.assert_markers_select_one_line(lines, freezes)

    def test_combination_that_cannot_factor_lists_environments(self) -> None:
        odd = _env(LINUX, (3, 11))
        freezes = _freezes(odd={env: ("1.0" if env == odd else "2.0") for env in ALL_ENVS})
        lines = mc.merge_constraints(freezes, direct=[])
        self.assertIn('odd==1.0; sys_platform == "linux" and python_version == "3.11"', lines)
        rest = [line for line in lines if line.startswith("odd==2.0; ")]
        self.assertEqual(len(rest), 1)
        self.assertIn('(sys_platform == "win32" and python_version == "3.11") or (', rest[0])
        self.assert_markers_select_one_line(lines, freezes)

    def test_python_and_platform_together(self) -> None:
        # Linux は Python の版で分かれ、Windows は1つ: 「OS と Python の版の両方」の書き方で表せる
        envs = [_env(platform, python) for platform in (LINUX, WINDOWS) for python in ((3, 11), (3, 13))]
        versions = {_env(LINUX, (3, 11)): "1.0", _env(LINUX, (3, 13)): "1.1"}
        freezes = {env: {"both": versions.get(env, "2.0")} for env in envs}
        lines = mc.merge_constraints(freezes, direct=[])
        self.assertEqual(
            lines,
            [
                'both==1.0; python_version < "3.13" and sys_platform == "linux"',
                'both==2.0; sys_platform == "win32"',
                'both==1.1; python_version >= "3.13" and sys_platform == "linux"',
            ],
        )
        self.assert_markers_select_one_line(lines, freezes)

    def test_newer_python_than_generated_uses_newest_line(self) -> None:
        if Marker is None:
            self.skipTest("packaging が無いため環境マーカーを評価できません")
        freezes = _freezes(
            contourpy={env: ("1.3.3" if env.python == (3, 11) else "1.4.0") for env in ALL_ENVS}
        )
        newest = [line for line in mc.merge_constraints(freezes, direct=[]) if line.startswith("contourpy==1.4.0")]
        marker = newest[0].split(";", 1)[1]
        self.assertTrue(Marker(marker).evaluate({"python_version": "3.14", "sys_platform": "linux"}))

    def test_direct_and_bootstrap_packages_are_not_written(self) -> None:
        freezes = _freezes(**{"discord-py": {env: "2.7.1" for env in ALL_ENVS}, "Pillow": {}})
        for env in ALL_ENVS:
            freezes[env]["pillow"] = "13.0.0"
            freezes[env]["wheel"] = "0.45.1"
        requirements = "discord.py[voice]==2.7.1\n# コメント\nPillow==13.0.0  # 画像\n-c constraints.txt\n"
        lines = mc.merge_constraints(freezes, direct=mc.direct_dependency_names(requirements))
        self.assertEqual(lines, ["aiohttp==3.14.4"])

    def test_direct_dependency_names_are_normalized(self) -> None:
        names = mc.direct_dependency_names(
            "discord.py[voice]==2.7.1\nPython_Dotenv==1.2.4\n\n--constraint constraints.txt\n-r other.txt\n"
        )
        self.assertEqual(names, {"discord-py", "python-dotenv"})


class InputTests(unittest.TestCase):
    def test_freeze_rejects_lines_that_are_not_pinned(self) -> None:
        for line in ("-e git+https://example.invalid/repo.git#egg=x", "pkg @ file:///tmp/pkg.whl", "pkg>=1.0"):
            with self.subTest(line=line):
                with self.assertRaises(mc.ConstraintsError):
                    mc.parse_freeze(f"aiohttp==3.14.4\n{line}\n", source="Linux-3.11.txt")

    def test_freeze_normalizes_names(self) -> None:
        self.assertEqual(
            mc.parse_freeze("# 見出し\nPydantic_Core==2.50.1\n\n", source="x"), {"pydantic-core": "2.50.1"}
        )

    def test_environment_label(self) -> None:
        self.assertEqual(mc.parse_environment_label("Windows-3.13"), _env(WINDOWS, (3, 13)))
        self.assertEqual(mc.parse_environment_label("Linux-3.11"), _env(LINUX, (3, 11)))
        for bad in ("linux-3.11", "Linux-311", "Ubuntu-3.12", "resolved-requirements"):
            with self.subTest(label=bad):
                with self.assertRaises(mc.ConstraintsError):
                    mc.parse_environment_label(bad)

    def test_no_input_is_an_error(self) -> None:
        with self.assertRaises(mc.ConstraintsError):
            mc.merge_constraints({}, direct=[])


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="constraints-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("matplotlib==3.10.8\n-c constraints.txt\n", encoding="utf-8")
        self.output = self.root / "constraints.txt"

    def write_freeze(self, label: str, text: str) -> Path:
        path = self.root / f"{label}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def run_main(self, *files: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mc.main(["--requirements", str(self.requirements), "--output", str(self.output), *map(str, files)])
        return code, out.getvalue(), err.getvalue()

    def test_writes_header_environments_and_pins_with_lf(self) -> None:
        files = [
            self.write_freeze("Windows-3.13", "matplotlib==3.10.8\ncontourpy==1.4.0\ncolorama==0.4.6\npip==26.2.1\n"),
            self.write_freeze("Linux-3.11", "matplotlib==3.10.8\ncontourpy==1.3.3\npip==26.2.1\nsetuptools==79.0.1\n"),
        ]
        code, out, err = self.run_main(*files)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("3 件", out)
        data = self.output.read_bytes()
        self.assertNotIn(b"\r\n", data)
        lines = data.decode("utf-8").splitlines()
        self.assertEqual(lines[: len(mc.HEADER)], list(mc.HEADER))
        self.assertEqual(lines[len(mc.HEADER)], "# 生成した環境: Linux-3.11, Windows-3.13")
        self.assertEqual(
            lines[len(mc.HEADER) + 1 :],
            [
                "colorama==0.4.6",
                'contourpy==1.3.3; python_version < "3.13"',
                'contourpy==1.4.0; python_version >= "3.13"',
            ],
        )

    def test_errors_are_reported_without_writing(self) -> None:
        bad = self.write_freeze("Linux-3.11", "pkg @ file:///tmp/pkg.whl\n")
        code, _, err = self.run_main(bad)
        self.assertEqual(code, 1)
        self.assertIn("constraints.txt を作れませんでした", err)
        self.assertFalse(self.output.exists())

    def test_duplicate_environment_is_an_error(self) -> None:
        first = self.write_freeze("Linux-3.11", "aiohttp==3.14.4\n")
        sub = self.root / "again"
        sub.mkdir()
        second = sub / "Linux-3.11.txt"
        second.write_text("aiohttp==3.14.4\n", encoding="utf-8")
        code, _, err = self.run_main(first, second)
        self.assertEqual(code, 1)
        self.assertIn("2つ", err)


if __name__ == "__main__":
    unittest.main()
