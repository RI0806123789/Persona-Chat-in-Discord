"""旧 config_private.py / config.py から .env を自動生成する移行スクリプト。

ver0.1.6 で APIキーの管理を `index/config_private.py` から
プロジェクトルート直下の `.env` へ移行した。既に稼働している環境には
`config_private.py`（gitignore 済みのため `git pull` しても消えない）が
残っているので、そこから値を取り出して `.env` を作る。

一度きりの移行作業のためのスクリプトで、ボット本体からは呼ばれない。
`.env` を作り終えたら `index/config_private.py` は削除してよい。

使い方（プロジェクトのどこから実行してもよい）:

    python3 deploy/migrate_config_to_env.py              # .env を作る
    python3 deploy/migrate_config_to_env.py --dry-run    # 何が書かれるか確認するだけ
    python3 deploy/migrate_config_to_env.py --force      # 既存の .env を上書きする
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

# このスクリプトは deploy/ に置かれているため、親がプロジェクトルートになる。
# カレントディレクトリではなく __file__ を基準にすることで、どこから実行しても動く。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 旧ファイルの置き場所。ソースのフォルダは ver0.1.7 で index/ から src/ へ改名したが、
# config_private.py は Git 管理外のため git pull では移動せず、旧フォルダ index/ に残る。
# **ここを src/ に変えないこと**（移行元のファイルが見つからなくなる）。
INDEX_DIR = PROJECT_ROOT / "index"
ENV_PATH = PROJECT_ROOT / ".env"

# 旧 settings_loader は「config_private.py があればそれ、無ければ config.py」の
# 優先順位だった。移行でも同じ順序を踏襲する（先に見つかった方を採用）。
SOURCE_FILES = (
    INDEX_DIR / "config_private.py",
    INDEX_DIR / "config.py",
)

# .env に書き出すキー。値は旧ファイルの変数名と同名で取り出す。
REQUIRED_KEYS = ("API_KEY_GEMINI", "TOKEN_DISCORD")

# config.py（コミット済みのテンプレート）に入っているプレースホルダ。
# これを .env へ書き写すと「設定したつもりで動かない」状態になるため未設定として扱う。
# src/settings_loader.py の _PLACEHOLDER_VALUES と同じ値を持つこと。
PLACEHOLDER_VALUES = {
    "YOUR_GEMINI_API_KEY",
    "YOUR_DISCORD_BOT_TOKEN",
}

# クォート無しでそのまま書ける文字の集合。APIキーやトークンは英数字と
# ごく一部の記号だけで構成されるため通常はここに収まる。外れる文字
# （空白や # など）が混じった場合だけクォートで囲む。
_SAFE_VALUE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._:/+-@"
)


def _mask(value: str) -> str:
    """キーを画面に出すための伏せ字。値そのものはログに残さない。"""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _extract_keys(path: Path) -> dict[str, str]:
    """.py ファイルから、トップレベルの文字列代入だけを取り出す。

    import して読む方法は取らない。import はファイルの中身を**実行する**ため、
    書き換えられていた場合に任意のコードが動いてしまう。移行に必要なのは
    「変数に代入された文字列」だけなので、ast で構文解析して読むだけにする。
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  ! 読み込めませんでした: {exc}")
        return {}

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        print(f"  ! Python として解釈できませんでした（{exc.lineno}行目付近）: {exc.msg}")
        return {}

    found: dict[str, str] = {}
    for node in tree.body:
        # `API_KEY_GEMINI = "..."` の形だけを拾う。
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in REQUIRED_KEYS:
                found[target.id] = node.value.value
    return found


def _is_usable(value: str) -> bool:
    """実際に使える値か。空・空白のみ・プレースホルダは未設定とみなす。"""
    stripped = value.strip()
    return bool(stripped) and stripped not in PLACEHOLDER_VALUES


def _format_value(value: str) -> str:
    """.env の右辺として安全な形に整える。

    python-dotenv はクォート無しの値に含まれる ` #` 以降をコメントとして
    切り落とすため、想定外の文字が混じっている場合はクォートで囲む。
    """
    if value and all(char in _SAFE_VALUE_CHARS for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _collect() -> dict[str, str]:
    """旧ファイル群を優先順に読み、キーごとに最初に見つかった有効な値を採用する。"""
    collected: dict[str, str] = {}

    for source in SOURCE_FILES:
        if not source.exists():
            print(f"- {source.name}: 見つかりません（スキップ）")
            continue

        print(f"- {source.name}: 読み込み中")
        for name, value in _extract_keys(source).items():
            # 既に優先度の高いファイルから取れているキーは上書きしない。
            if name in collected:
                continue
            if not _is_usable(value):
                print(f"    {name}: プレースホルダまたは空のため採用しません")
                continue
            collected[name] = value.strip()
            print(f"    {name}: 採用しました（{_mask(collected[name])} / {len(collected[name])}文字）")

    return collected


def _build_env_text(values: dict[str, str]) -> str:
    """.env の中身を組み立てる。書式は .env.example に合わせる。"""
    lines = [
        "# このファイルは deploy/migrate_config_to_env.py が",
        "# 旧 config_private.py / config.py から自動生成しました。",
        "# 実際のAPIキーを含みます。.gitignore で除外済みのためコミットされませんが、",
        "# 共有・スクリーンショット・貼り付けには注意してください。",
        "# 書式や取得先の説明は .env.example を参照。",
        "",
        "# GEMINI の API キー",
        f"API_KEY_GEMINI={_format_value(values['API_KEY_GEMINI'])}",
        "",
        "# Discord のボットトークン",
        f"TOKEN_DISCORD={_format_value(values['TOKEN_DISCORD'])}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="旧 config_private.py / config.py から .env を生成します。"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存の .env を上書きする（既定では中断します）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイルを書かず、何が生成されるかだけ表示する",
    )
    args = parser.parse_args()

    print(f"プロジェクトルート: {PROJECT_ROOT}")
    print(f"出力先: {ENV_PATH}")
    print("")

    # 上書き確認は値を読む前に行う。読み込みログを出したあとで中断すると
    # 「作られたのか作られていないのか」が分かりにくいため。
    if ENV_PATH.exists() and not args.force and not args.dry_run:
        print("中断: .env は既に存在します。")
        print("  上書きする場合は --force を付けてください。")
        print("  先に中身を確認したい場合は --dry-run を使ってください。")
        return 1

    values = _collect()
    print("")

    missing = [name for name in REQUIRED_KEYS if name not in values]
    if missing:
        print("キー未設定")
        print("")
        print("旧ファイルから次のキーを取り出せませんでした:")
        for name in missing:
            print(f"  - {name}")
        print("")
        print("index/config_private.py が存在するか、実際の値が書かれているかを確認してください。")
        print("旧ファイルが残っていない場合は、.env.example をコピーして手で記入してください。")
        return 1

    env_text = _build_env_text(values)

    if args.dry_run:
        print("--dry-run のためファイルは書き込みません。生成される内容（値は伏せ字）:")
        print("")
        for line in env_text.splitlines():
            for name, value in values.items():
                line = line.replace(value, _mask(value))
            print(f"  {line}")
        return 0

    # 改行は LF に固定する（Windows で生成して Linux へ持って行っても崩れないように）。
    ENV_PATH.write_text(env_text, encoding="utf-8", newline="\n")

    # 秘密情報のファイルなので本人だけが読める権限にする。
    # Windows ではこの権限モデルが無く実質的に無視されるが、エラーにはならない。
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError as exc:
        print(f"注意: パーミッションを 600 に変更できませんでした: {exc}")

    print(f".env を作成しました: {ENV_PATH}")
    print("")
    print("次の手順:")
    print("  1. cd src && python3 -c \"from settings_loader import load_settings; load_settings()\"")
    print("     → 何も表示されなければ読み込み成功（キー未設定なら 'キー未設定' と出ます）")
    print("  2. 動作確認できたら index/config_private.py は削除して構いません")
    print("  3. systemd 運用中なら sudo systemctl restart bot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
