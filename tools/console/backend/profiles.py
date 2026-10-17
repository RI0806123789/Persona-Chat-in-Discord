"""接続先（プロファイル）の読み込み。

ブラウザからは「どのプロファイルに接続するか（id）」だけを受け取り、
ホスト名・鍵・known_hosts はこのモジュールが読む JSON からしか決めない。
（ブラウザから任意のホストを指定できると、このバックエンドを踏み台にされる危険があるため）

profiles.json があればそれを、無ければ profiles.example.json を読む。
見本の値（YOUR_ で始まる文字列）が残っている接続先には接続しない（unset_fields で見分ける）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROFILES_PATH = BACKEND_DIR / "profiles.json"
EXAMPLE_PROFILES_PATH = BACKEND_DIR / "profiles.example.json"

ENVIRONMENTS = ("development", "production")

# サービス名に使ってよい文字（シェルに渡すため、記号を厳しく制限する）
_SAFE_SERVICE_NAME = re.compile(r"^[A-Za-z0-9@._-]+$")

# 見本（profiles.example.json）で「書き換えてください」の印にしている値。.env.example の YOUR_GEMINI_API_KEY と同じ流儀。
# ★ 見本の仮の値のまま接続させないこと: 仮のホスト名を DNS が偶然引けると、知らないサーバーへつなぎに行ってしまう
#   （ホスト鍵の照合でログイン前に止まるが、接続しに行った記録は相手に残る）。
# 見本の値の書き方を変えたら、この形と examples/ の見本もそろえて直すこと。
_PLACEHOLDER = re.compile(r"YOUR_[A-Z0-9_]+")


class ProfileError(Exception):
    """プロファイルの書き方が間違っているときのエラー。"""


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    environment: str
    host: str
    port: int | None
    username: str | None
    key_path: str | None
    known_hosts: str | None
    use_ssh_config: bool
    service: str
    bot_dir: str
    journal_sudo: bool
    note: str

    @property
    def unset_fields(self) -> list[str]:
        """見本の値（YOUR_...）のまま書き換えられていない項目の名前。空なら設定済み。"""
        values = {
            "host": self.host,
            "username": self.username,
            "key_path": self.key_path,
            "known_hosts": self.known_hosts,
            "bot_dir": self.bot_dir,
        }
        return [name for name, value in values.items() if value and _PLACEHOLDER.search(value)]

    def public_dict(self) -> dict:
        """画面へ返す情報。鍵ファイルの場所は返さない。"""
        return {
            "id": self.id,
            "label": self.label,
            "environment": self.environment,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "service": self.service,
            "bot_dir": self.bot_dir,
            "note": self.note,
            "unset_fields": self.unset_fields,
        }


def resolve_local_path(path_text: str) -> Path:
    """手元（Windows 側）のパスを解決する。相対パスは backend/ を基準にする。"""
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return path


def _optional_str(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProfileError(f"{key} は文字列か null で書いてください。")
    return value


def _parse_profile(raw: dict) -> Profile:
    if not isinstance(raw, dict):
        raise ProfileError("プロファイルは { ... } の形で書いてください。")

    profile_id = raw.get("id")
    host = raw.get("host")
    if not isinstance(profile_id, str) or not profile_id:
        raise ProfileError("id が空のプロファイルがあります。")
    if not isinstance(host, str) or not host:
        raise ProfileError(f"プロファイル {profile_id} の host が空です。")

    port = raw.get("port")
    if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
        raise ProfileError(f"プロファイル {profile_id} の port は 1〜65535 の数値か null にしてください。")

    environment = raw.get("environment", "production")
    if environment not in ENVIRONMENTS:
        raise ProfileError(f"プロファイル {profile_id} の environment は development か production にしてください。")

    service = raw.get("service", "bot")
    if not isinstance(service, str) or not _SAFE_SERVICE_NAME.match(service):
        raise ProfileError(f"プロファイル {profile_id} の service に使えない文字が含まれています。")

    bot_dir = raw.get("bot_dir", "/home/ubuntu/Persona-Chat-in-Discord")
    if not isinstance(bot_dir, str) or not bot_dir.startswith("/"):
        raise ProfileError(f"プロファイル {profile_id} の bot_dir はサーバー上の絶対パスで書いてください。")

    return Profile(
        id=profile_id,
        label=str(raw.get("label") or profile_id),
        environment=environment,
        host=host,
        port=port,
        username=_optional_str(raw, "username"),
        key_path=_optional_str(raw, "key_path"),
        known_hosts=_optional_str(raw, "known_hosts"),
        use_ssh_config=bool(raw.get("use_ssh_config", True)),
        service=service,
        bot_dir=bot_dir.rstrip("/") or "/",
        journal_sudo=bool(raw.get("journal_sudo", False)),
        note=str(raw.get("note") or ""),
    )


def load_profiles() -> tuple[dict[str, Profile], str]:
    """プロファイルを読み込み、(id → Profile の辞書, 読んだファイル名) を返す。

    画面から接続するたびに読み直すので、JSON を書き換えたらバックエンドの再起動は要らない。
    """
    path = PROFILES_PATH if PROFILES_PATH.exists() else EXAMPLE_PROFILES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ProfileError(f"{path.name} が見つかりません。") from error
    except json.JSONDecodeError as error:
        raise ProfileError(f"{path.name} が JSON として読めません（{error.lineno} 行目）。") from error

    raw_profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(raw_profiles, list):
        raise ProfileError(f"{path.name} には \"profiles\": [ ... ] を書いてください。")

    profiles: dict[str, Profile] = {}
    for raw in raw_profiles:
        profile = _parse_profile(raw)
        if profile.id in profiles:
            raise ProfileError(f"id が重複しています: {profile.id}")
        profiles[profile.id] = profile
    return profiles, path.name
