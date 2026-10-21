"""ペルソナ（キャラクター設定）の一覧。

Git が管理するのは `prompts/` の書式見本（テンプレート）だけで、キャラ別のプロンプトは
利用者が各自のパソコン・サーバーで作る。ここでは `prompts/` にある `.txt` を選択肢として
集め、任意の `prompts/personas.json` から表示名・読み上げの声・既定のペルソナを読む。

personas.json の書式見本は `prompts/00_prompt_format.json`（`PERSONA_SETTINGS_TEMPLATE_FILE_NAME`）で、
利用者が personas.json へコピーして書き換える元。ここでは見本を読まない。personas.json の
最上位は `default` と `personas` だけを使い、それ以外（見本の説明欄 `_説明` など）は警告せずに
無視する（見本をコピーしたまま使っても警告が出ないようにするため）。

**保存・選択に使う値は旧版と同じ「src/ 基準の相対パス」（`"../prompts/<ファイル名>"`）。**
ファイル名を変えなければ、DB に保存済みのサーバー設定がそのまま引き継がれる。

**空文字の値は「既定のペルソナに従う」。** サーバーでまだ選んでいない状態を表し、
personas.json の `default` → 先頭のファイル → 組み込みの標準（`BUILTIN_PERSONA_PROMPT`）の
順に解決する。保存済みのファイルが見つからない間も値は消さずに既定で応答し、ファイルを
戻せば元のキャラに戻る（Git の更新でファイルが一時的に消えても設定を失わないため）。

一覧は起動時（`bot.py`）とキャラ設定パネルを開いたときに読み直し、スナップショットを
差し替える。読み込み途中の状態は公開しない。応答・読み上げ・表示はスナップショットだけを
参照するため、イベントループ上でファイルを読まない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from persona_bot.config import constants
from persona_bot.config.constants import (
    DISCORD_SELECT_VALUE_MAX_LENGTH,
    PERSONA_SETTINGS_TEMPLATE_FILE_NAME,
    PERSONA_VALUE_PREFIX,
    PROMPT_TEMPLATE_FILE_NAME,
    VOICE_PROFILE_FALLBACK,
)
from persona_bot.config.paths import resolve_prompt_path

# 「既定のペルソナに従う」を表す値。DB の未設定（空文字）と同じ。
FOLLOW_DEFAULT_PERSONA = ""
# ペルソナを1つも作っていない間の表示名（組み込みの標準プロンプトで応答している）。
BUILTIN_PERSONA_DISPLAY_NAME = "標準（組み込み）"

# edge-tts が受け付けるパラメータの形式（パッケージ内部の検証と同じもの）。
# 形式が違うと edge_tts.Communicate が ValueError を投げ、黙って gTTS へ退避し続ける
# （音は出るのでキャラ差が消えたことに気づきにくい）ため、読み込み時に点検する。
_EDGE_RATE_PATTERN = re.compile(r"^[+-]\d+%$")
_EDGE_PITCH_PATTERN = re.compile(r"^[+-]\d+Hz$")
_VOICE_KEYS = ("voice", "pitch", "rate", "eq")


@dataclass(frozen=True)
class Persona:
    """選択肢に出すペルソナ1件。"""

    value: str
    name: str
    voice: Mapping[str, Any]


@dataclass(frozen=True)
class PersonaCatalog:
    """読み込んだペルソナの一覧（不変のスナップショット）。"""

    personas: tuple[Persona, ...] = ()
    default_value: str = FOLLOW_DEFAULT_PERSONA
    _by_value: Mapping[str, Persona] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_value", MappingProxyType({persona.value: persona for persona in self.personas}))

    def get(self, value: str | None) -> Persona | None:
        return self._by_value.get(value) if isinstance(value, str) else None

    def contains(self, value: str | None) -> bool:
        return self.get(value) is not None

    def resolve(self, value: str | None) -> str:
        """応答に使う値を返す。未設定・見つからないファイルは既定のペルソナにする。"""
        return value if self.contains(value) else self.default_value

    def display_name(self, value: str | None) -> str:
        persona = self.get(value)
        if persona is not None:
            return persona.name
        if not value:
            return BUILTIN_PERSONA_DISPLAY_NAME
        # 一覧に無い値（消したファイルの評価記録など）はファイル名で見せる
        return Path(value).stem or str(value)

    def voice_profile(self, value: str | None) -> Mapping[str, Any]:
        persona = self.get(value)
        return persona.voice if persona is not None else VOICE_PROFILE_FALLBACK


_catalog = PersonaCatalog()


def current_persona_catalog() -> PersonaCatalog:
    """最後に読み込んだ一覧を返す（ファイルは読まない）。未読込なら空の一覧。"""
    return _catalog


def set_persona_catalog(catalog: PersonaCatalog) -> None:
    global _catalog
    _catalog = catalog


def persona_value(file_name: str) -> str:
    return PERSONA_VALUE_PREFIX + file_name


def persona_file_name(value: Any) -> str | None:
    """値がペルソナの形（"../prompts/<名前>.txt"）ならファイル名を返す。

    フォルダの区切り・テンプレート・隠しファイルを含む値は拒否する。保存値や
    セレクトの値から prompts/ の外（.env など）を読ませないための関門。
    """
    if not isinstance(value, str) or not value.startswith(PERSONA_VALUE_PREFIX):
        return None
    name = value[len(PERSONA_VALUE_PREFIX):]
    if not _is_persona_file_name(name):
        return None
    return name


def _is_persona_file_name(name: str) -> bool:
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
        and name != PROMPT_TEMPLATE_FILE_NAME
        and Path(name).suffix.lower() == ".txt"
    )


def is_selectable_persona(value: str) -> bool:
    """確定してよいペルソナか（ファイルを読むので別スレッドで呼ぶ）。"""
    if value == FOLLOW_DEFAULT_PERSONA:
        return True
    if persona_file_name(value) is None:
        return False
    try:
        return resolve_prompt_path(value).is_file()
    except OSError:
        return False


def load_persona_catalog(prompts_dir: Path | None = None, settings_path: Path | None = None) -> PersonaCatalog:
    """prompts/ と personas.json から一覧を作る（ファイルを読むので別スレッドで呼ぶ）。

    フォルダが無いときは空の一覧を返す。フォルダを読めないときは OSError を送出し、
    呼び出し側は前回の一覧を使い続ける。personas.json の誤りは警告して無視する
    （ファイルは書き換えず、そのまま残す）。
    """
    prompts_dir = constants.PROMPTS_DIR if prompts_dir is None else prompts_dir
    settings_path = constants.PERSONA_SETTINGS_PATH if settings_path is None else settings_path
    files = _scan_persona_files(prompts_dir)
    settings = _load_persona_settings(settings_path)

    entries = settings.get("personas")
    if entries is None:
        entries = {}
    elif not isinstance(entries, dict):
        print("警告: personas.json の personas はオブジェクト（{ }）で書いてください。表示名と声の設定を使わずに続行します。")
        entries = {}

    # personas.json に書いた順を優先し、書いていないファイルはファイル名の順に続ける
    ordered = [name for name in entries if name in files]
    ordered += sorted(name for name in files if name not in entries)
    for name in entries:
        if name not in files:
            print(f"警告: personas.json の「{name}」に対応するファイルが prompts/ にありません。")

    personas = tuple(_build_persona(name, entries.get(name)) for name in ordered)
    return PersonaCatalog(personas, _resolve_default(settings.get("default"), personas))


def reload_persona_catalog() -> PersonaCatalog:
    """一覧を読み直して差し替える（起動時用。読めなければ前回の一覧を保つ）。"""
    try:
        catalog = load_persona_catalog()
    except OSError as error:
        print(f"警告: prompts/ を読めませんでした（{type(error).__name__}）。前回のペルソナ一覧を使います。")
        return _catalog
    set_persona_catalog(catalog)
    if not catalog.personas:
        print(
            "ペルソナ: prompts/ に利用できるペルソナがありません。"
            f"prompts/{PROMPT_TEMPLATE_FILE_NAME} をコピーして prompts/<名前>.txt を作ると、"
            "キャラ設定から選べます（作るまでは組み込みの標準プロンプトで応答します）。"
            f"表示名と声は prompts/{PERSONA_SETTINGS_TEMPLATE_FILE_NAME} を prompts/personas.json に"
            "コピーして決められます。"
        )
    else:
        print(f"ペルソナ: {len(catalog.personas)} 件を読み込みました。")
    return catalog


async def refresh_persona_catalog() -> PersonaCatalog:
    """一覧を別スレッドで読み直して差し替える（キャラ設定パネルを開くとき用）。"""
    from persona_bot.core.background import run_in_thread

    try:
        catalog = await run_in_thread(load_persona_catalog)
    except OSError as error:
        print(f"警告: prompts/ を読めませんでした（{type(error).__name__}）。前回のペルソナ一覧を使います。")
        return _catalog
    set_persona_catalog(catalog)
    return catalog


def _scan_persona_files(prompts_dir: Path) -> set[str]:
    if not prompts_dir.is_dir():
        return set()
    names: set[str] = set()
    for path in prompts_dir.iterdir():
        name = path.name
        if not _is_persona_file_name(name) or not path.is_file():
            continue
        if len(persona_value(name)) > DISCORD_SELECT_VALUE_MAX_LENGTH:
            print(f"警告: ペルソナのファイル名が長すぎるため選択肢に出せません（{name}）。短い名前に変えてください。")
            continue
        names.add(name)
    return names


def _load_persona_settings(settings_path: Path) -> dict[str, Any]:
    try:
        # 手書きのファイルなので、メモ帳の「UTF-8 (BOM 付き)」保存でも読めるようにする
        text = settings_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        print(f"警告: personas.json を読めませんでした（{type(error).__name__}）。表示名と声の設定を使わずに続行します。")
        return {}
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as error:
        print(
            f"警告: personas.json の {error.lineno} 行目付近が JSON として読めません。"
            "表示名と声の設定を使わずに続行します（ファイルはそのまま残します）。"
        )
        return {}
    if not isinstance(settings, dict):
        print("警告: personas.json の最上位はオブジェクト（{ }）で書いてください。表示名と声の設定を使わずに続行します。")
        return {}
    return settings


def _build_persona(file_name: str, entry: Any) -> Persona:
    if entry is not None and not isinstance(entry, dict):
        print(f"警告: personas.json の「{file_name}」はオブジェクト（{{ }}）で書いてください。既定の表示名と声を使います。")
        entry = {}
    entry = entry or {}
    name = entry.get("name")
    display = " ".join(name.split()) if isinstance(name, str) else ""
    if name is not None and not display:
        print(f"警告: personas.json の「{file_name}」の name が空か文字列ではないため、ファイル名を表示名にします。")
    return Persona(
        value=persona_value(file_name),
        name=display or Path(file_name).stem,
        voice=MappingProxyType(_build_voice(file_name, entry.get("voice"))),
    )


def _build_voice(file_name: str, voice: Any) -> dict[str, Any]:
    profile = dict(VOICE_PROFILE_FALLBACK)
    if voice is None:
        return profile
    if not isinstance(voice, dict):
        print(f"警告: personas.json の「{file_name}」の voice はオブジェクト（{{ }}）で書いてください。既定の声を使います。")
        return profile
    for key, value in voice.items():
        if key not in _VOICE_KEYS:
            print(f"警告: personas.json の「{file_name}」の voice に未対応の項目があります（{key}）。使える項目: {', '.join(_VOICE_KEYS)}")
            continue
        if not isinstance(value, str) or not value:
            print(f"警告: personas.json の「{file_name}」の voice.{key} は文字列で書いてください。既定値を使います。")
            continue
        if key == "rate" and not _EDGE_RATE_PATTERN.match(value):
            print(f"警告: personas.json の「{file_name}」の rate が不正です（{value!r}）。'+5%' のような形式で指定してください。既定値を使います。")
            continue
        if key == "pitch" and not _EDGE_PITCH_PATTERN.match(value):
            print(f"警告: personas.json の「{file_name}」の pitch が不正です（{value!r}）。'+10Hz' のような形式で指定してください。既定値を使います。")
            continue
        profile[key] = value
    return profile


def _resolve_default(default: Any, personas: tuple[Persona, ...]) -> str:
    values = {persona.value for persona in personas}
    if default is not None:
        if isinstance(default, str) and persona_value(default) in values:
            return persona_value(default)
        print("警告: personas.json の default に指定したファイルがありません。先頭のペルソナを既定にします。")
    return personas[0].value if personas else FOLLOW_DEFAULT_PERSONA
