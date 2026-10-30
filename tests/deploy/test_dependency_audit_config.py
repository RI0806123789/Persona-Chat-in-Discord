"""依存の更新提案と脆弱性検査の設定（Issue #285）が約束を守っていることを検証する。

PyYAML はボットの依存に無い（足さない）ため、YAML は解析せず、守るべき約束を文字列として確かめる。
GitHub 上での実際の動作（Dependabot の提案・Actions の実行）はここでは確かめられない。
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
GITHUB_DIR = ROOT / ".github"
DEPENDABOT_PATH = GITHUB_DIR / "dependabot.yml"
WORKFLOWS_DIR = GITHUB_DIR / "workflows"
AUDIT_WORKFLOW_PATH = WORKFLOWS_DIR / "dependency-audit.yml"
AUDIT_TOOL_REQUIREMENTS = GITHUB_DIR / "audit" / "requirements.txt"

# 「公開から2週間以上たった版に固定する」方針（tools/console/backend/requirements.txt・.npmrc と同じ）
MIN_COOLDOWN_DAYS = 14

# 更新を提案させる対象: (エコシステム, ディレクトリ) → そこに無ければならない依存の定義ファイル
EXPECTED_UPDATE_TARGETS = {
    ("pip", "/"): ("requirements.txt",),
    ("pip", "/tools/console/backend"): ("requirements.txt",),
    ("pip", "/.github/audit"): ("requirements.txt",),
    ("npm", "/tools/console/frontend"): ("package.json", "package-lock.json"),
    ("github-actions", "/"): (".github/workflows",),
}

# 変更されたら検査を走らせる依存の定義ファイル（push と pull_request の両方）
EXPECTED_AUDIT_PATHS = {
    "requirements.txt",
    "tools/console/backend/requirements.txt",
    "tools/console/frontend/package.json",
    "tools/console/frontend/package-lock.json",
    ".github/audit/requirements.txt",
    ".github/workflows/dependency-audit.yml",
}

# pip-audit で照合する requirements（ボット本体と管理コンソールのバックエンド）
EXPECTED_PIP_AUDIT_TARGETS = ("requirements.txt", "tools/console/backend/requirements.txt")

_SHA_PINNED_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _read(path: Path) -> str:
    # read_text は改行を \n にそろえるので、Windows の CRLF のチェックアウトでも同じに読める
    return path.read_text(encoding="utf-8")


def _dependabot_entries() -> list[str]:
    """dependabot.yml の updates を、`- package-ecosystem:` ごとの塊に分ける。"""
    text = _read(DEPENDABOT_PATH)
    starts = [match.start() for match in re.finditer(r"^  - package-ecosystem:", text, re.MULTILINE)]
    return [text[start:end] for start, end in zip(starts, starts[1:] + [len(text)])]


def _entry_ecosystem(entry: str) -> str:
    return re.match(r"  - package-ecosystem:\s*(\S+)", entry).group(1)


def _entry_directories(entry: str) -> list[str]:
    single = re.search(r'^    directory:\s*"([^"]+)"', entry, re.MULTILINE)
    if single:
        return [single.group(1)]
    listed = re.search(r'^    directories:\n((?:      - "[^"]+"\n)+)', entry, re.MULTILINE)
    if listed:
        return re.findall(r'"([^"]+)"', listed.group(1))
    return []


def _uses_lines(text: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.MULTILINE)]


class DependabotConfigTests(unittest.TestCase):
    def test_every_manifest_gets_update_proposals(self) -> None:
        """ボット・管理コンソール（バックエンドと画面）・検査ツール・actions のすべてが対象になる。"""
        text = _read(DEPENDABOT_PATH)
        self.assertRegex(text, r"(?m)^version: 2$")
        targets = {
            (_entry_ecosystem(entry), directory)
            for entry in _dependabot_entries()
            for directory in _entry_directories(entry)
        }
        self.assertEqual(targets, set(EXPECTED_UPDATE_TARGETS))
        # ディレクトリを移したのに設定を直し忘れると、Dependabot は黙って何も提案しなくなる
        for (ecosystem, directory), manifests in EXPECTED_UPDATE_TARGETS.items():
            base = ROOT / directory.strip("/")
            for manifest in manifests:
                path = (ROOT / manifest) if ecosystem == "github-actions" else (base / manifest)
                with self.subTest(ecosystem=ecosystem, directory=directory, manifest=manifest):
                    self.assertTrue(path.exists(), f"{path.relative_to(ROOT)} がありません")

    def test_every_update_waits_two_weeks_after_release(self) -> None:
        """公開から2週間未満の版を提案させない（cooldown が抜けた更新対象を作らない）。"""
        entries = _dependabot_entries()
        self.assertTrue(entries)
        for entry in entries:
            ecosystem = _entry_ecosystem(entry)
            with self.subTest(ecosystem=ecosystem):
                self.assertIn("    cooldown:\n", entry)
                days = re.search(r"^      default-days:\s*(\d+)\s*$", entry, re.MULTILINE)
                self.assertIsNotNone(days, "cooldown に default-days がありません")
                self.assertGreaterEqual(int(days.group(1)), MIN_COOLDOWN_DAYS)
                if ecosystem == "github-actions":
                    # github-actions は semver-*-days に対応していない（書くと設定エラーになる）
                    self.assertNotIn("semver-", entry)

    def test_every_update_runs_weekly(self) -> None:
        for entry in _dependabot_entries():
            with self.subTest(ecosystem=_entry_ecosystem(entry)):
                self.assertRegex(entry, r"(?m)^      interval:\s*weekly\s*$")


class AuditWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read(AUDIT_WORKFLOW_PATH)

    def test_runs_read_only_without_secrets(self) -> None:
        """フォークからの PR でもシークレットなしで動く（読み取りだけの権限）。"""
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.text, r":\s*write\b")
        self.assertNotIn("secrets.", self.text)
        # pull_request_target はフォークの PR に書き込み権限とシークレットを渡してしまう
        self.assertNotIn("pull_request_target", self.text)
        checkouts = self.text.count("uses: actions/checkout@")
        self.assertGreater(checkouts, 0)
        self.assertEqual(self.text.count("persist-credentials: false"), checkouts)

    def test_runs_when_dependencies_change_and_every_week(self) -> None:
        path_lists = re.findall(r"^    paths:\n((?:      - .+\n)+)", self.text, re.MULTILINE)
        self.assertEqual(len(path_lists), 2, "push と pull_request の両方に paths が必要です")
        for path_list in path_lists:
            self.assertEqual(set(re.findall(r"      - (.+)", path_list)), EXPECTED_AUDIT_PATHS)
        for path in EXPECTED_AUDIT_PATHS:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), f"{path} がありません")
        # 依存を変えなくても新しい脆弱性は公開されるので、定期実行も必要
        self.assertRegex(self.text, r"(?m)^  schedule:\n    - cron: '[^']+'$")

    def test_audits_bot_console_and_frontend(self) -> None:
        loop = re.search(r"for target in ([^;]+); do", self.text)
        self.assertIsNotNone(loop)
        self.assertEqual(tuple(loop.group(1).split()), EXPECTED_PIP_AUDIT_TARGETS)
        self.assertIn('python -m pip_audit -r "$target"', self.text)
        # 推移依存が変わる OS と Python の版を、回帰テストと同じ組み合わせで照合する
        self.assertIn("os: [ubuntu-latest, windows-latest]", self.text)
        self.assertIn("python: ['3.11', '3.13']", self.text)
        self.assertIn("working-directory: tools/console/frontend", self.text)
        # 概要の見出しではなく、実行するコマンドそのものを確かめる
        commands = re.findall(r"^\s*if (npm audit\b[^>]*)>", self.text, re.MULTILINE)
        self.assertEqual([command.strip() for command in commands], ["npm audit --omit=dev"])

    def test_starts_as_warning_and_can_be_made_required(self) -> None:
        """誤検知を確かめるまでは警告だけにする。必須にするときは AUDIT_REQUIRED とこのテストを直す。"""
        self.assertRegex(self.text, r"(?m)^  AUDIT_REQUIRED: 'false'$")
        # 両方のジョブが、検出時の警告の注釈と、切り替え後だけの失敗を持つ
        self.assertEqual(self.text.count("::warning title=依存の脆弱性検査"), 2)
        self.assertEqual(self.text.count('[ "$failed" -ne 0 ] && [ "$AUDIT_REQUIRED" = "true" ]'), 2)
        self.assertNotIn("continue-on-error", self.text)


class AuditToolPinTests(unittest.TestCase):
    def test_pip_audit_is_pinned_for_ci_only(self) -> None:
        """pip-audit は CI でだけ使う。版を固定し、ボット・管理コンソールの依存には入れない。"""
        requirements = [
            line.strip()
            for line in _read(AUDIT_TOOL_REQUIREMENTS).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(requirements), 1)
        self.assertRegex(requirements[0], r"^pip-audit==\d+(\.\d+)+$")
        self.assertIn(
            "python -m pip install -r .github/audit/requirements.txt", _read(AUDIT_WORKFLOW_PATH)
        )
        for runtime_requirements in EXPECTED_PIP_AUDIT_TARGETS:
            with self.subTest(requirements=runtime_requirements):
                self.assertNotRegex(_read(ROOT / runtime_requirements).lower(), r"pip[-_]audit")


class WorkflowPinningTests(unittest.TestCase):
    def test_every_action_is_pinned_by_commit_sha(self) -> None:
        """タグは付け替えられるので、actions はコミットSHAで固定する（Dependabot も SHA で更新する）。"""
        workflows = sorted(WORKFLOWS_DIR.glob("*.yml"))
        self.assertIn(AUDIT_WORKFLOW_PATH, workflows)
        for workflow in workflows:
            uses = _uses_lines(_read(workflow))
            self.assertTrue(uses, f"{workflow.name} に uses がありません")
            for action in uses:
                with self.subTest(workflow=workflow.name, action=action):
                    self.assertRegex(action, _SHA_PINNED_USES)


if __name__ == "__main__":
    unittest.main()
