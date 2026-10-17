"""手元のリポジトリ（このダッシュボードが入っているボットのリポジトリ）から読む情報。

ダッシュボードは tools/console/ にあるので、ボットのソースは ../../src にある。
★ ボットのコードは import しない（中身が実行され、設定の読み込みや DB の初期化が走るため）。ast で構文として読むだけ。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATABASE_PY = REPO_ROOT / "src" / "persona_bot" / "core" / "database.py"


def local_schema_version(path: Path = DATABASE_PY) -> int | None:
    """database.py の SCHEMA_VERSION（手元の最新のスキーマの版）。読めなければ None。

    本番の bot.db の版（PRAGMA user_version）と比べ、新しい版がまだデプロイされていないかを画面に出すために使う。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        else:
            continue
        if "SCHEMA_VERSION" in names and isinstance(value, ast.Constant) and type(value.value) is int:
            return value.value
    return None
