"""SQLite データベース層（memory/bot.db）。

これまで memory/ 配下の JSON / テキストファイルで持っていた以下のデータを
SQLite で管理する。SQL 文はすべてこのモジュールに集約する。

  - affinity     … ユーザーごとの親密度・感情（旧 affinity.json）
  - user_stats   … ユーザー別会話統計（旧 user_stats.json）
  - queue_tasks  … 文書処理タスクキュー（旧 queue.json）
  - pending_log  … 記憶圧縮前の一時会話ログ（旧 pending_log.txt）
  - profile      … プロフィール記憶（トップレベルキーごとに1行、値はJSON文字列）

projects.json は LLM が構造を自由に決める JSON のため、
テーブル化せず従来どおりファイルのまま扱う。

■ 旧データの取り込み
初回起動時、旧ファイルが残っていれば内容を DB へ取り込み、二重取り込みを
防ぐためファイルを「<元の名前>.imported.bak」に改名して残す（バックアップ兼用）。

■ profile.json は「手書き追加用の受け皿」
profile.json は毎回の起動時にチェックされ、中身があれば profile テーブルへ
マージ（同名キーはファイル側で上書き）した後、ファイルを {} に戻す。
つまりユーザーが profile.json に事実を書き足してボットを再起動するだけで、
その内容が自動的にデータベースの記憶へ追加される。

■ スキーマを後から変更したくなったら（列の追加など）
SCHEMA_VERSION を 1 つ上げ、_MIGRATIONS にそのバージョン番号で ALTER TABLE 文を
追記するだけでよい。適用済みバージョンは DB 内の PRAGMA user_version に記録され、
起動時に未適用の分だけが順番に実行される（二重適用されない）。
_CREATE_TABLE_STATEMENTS は初版（バージョン1）の形のまま変更しないこと —
列の追加・変更は必ず _MIGRATIONS 側で行う（新規 DB にも同じ移行が走るため、
両方を書き換えると二重適用でエラーになる）。
"""

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

# 現在のスキーマバージョン。スキーマを変更したら 1 つ上げる。
SCHEMA_VERSION = 2

# バージョン番号 → そのバージョンへ上げるための SQL 文リスト。
# 例) user_stats に列を追加したくなったら:
#   SCHEMA_VERSION = 3 に上げて、ここに
#   3: ["ALTER TABLE user_stats ADD COLUMN favorite_emoji TEXT NOT NULL DEFAULT ''"],
#   のように追記する。既存 DB・新規 DB の両方に自動適用される。
_MIGRATIONS: dict[int, list[str]] = {
    # バージョン2: プロフィール記憶を profile.json から DB へ移行
    2: [
        """CREATE TABLE IF NOT EXISTS profile (
            key   TEXT PRIMARY KEY,                    -- プロフィールのトップレベルキー
            value TEXT NOT NULL                        -- 値（JSON文字列）
        )"""
    ],
}

# 初版（バージョン1）のテーブル定義。ここは変更禁止（変更は _MIGRATIONS で）。
_CREATE_TABLE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS affinity (
        user_id          TEXT PRIMARY KEY,               -- Discord ユーザーID（文字列）
        affinity         INTEGER NOT NULL DEFAULT 50,    -- 親密度 0〜100
        valence          INTEGER NOT NULL DEFAULT 50,    -- 機嫌 0〜100
        arousal          INTEGER NOT NULL DEFAULT 50,    -- テンション 0〜100
        last_interaction REAL    NOT NULL DEFAULT 0      -- 最終対話時刻（UNIX秒）
    )""",
    """CREATE TABLE IF NOT EXISTS user_stats (
        user_id        TEXT PRIMARY KEY,                 -- Discord ユーザーID（文字列）
        user_name      TEXT NOT NULL DEFAULT '',
        total_count    INTEGER NOT NULL DEFAULT 0,       -- 累計発言回数
        hourly         TEXT NOT NULL DEFAULT '[]',       -- 時間帯別回数（24要素のJSON配列）
        first_seen     TEXT NOT NULL DEFAULT '',         -- ISO 8601
        last_seen      TEXT NOT NULL DEFAULT '',         -- ISO 8601
        recent_samples TEXT NOT NULL DEFAULT '[]',       -- 直近発言サンプル（JSON配列）
        analysis       TEXT                              -- LLM解析結果のJSON（未解析はNULL）
    )""",
    """CREATE TABLE IF NOT EXISTS queue_tasks (
        task_id          TEXT PRIMARY KEY,
        status           TEXT NOT NULL DEFAULT 'pending', -- pending/processing/completed/failed
        channel_id       INTEGER NOT NULL DEFAULT 0,
        message_id       INTEGER NOT NULL DEFAULT 0,
        author_id        INTEGER NOT NULL DEFAULT 0,
        question         TEXT NOT NULL DEFAULT '',
        attachment_count INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL DEFAULT '',        -- ISO 8601
        updated_at       TEXT NOT NULL DEFAULT '',        -- ISO 8601
        finished_at      TEXT,                            -- 終了時刻（未終了はNULL）
        error            TEXT                             -- 失敗理由（成功時はNULL）
    )""",
    """CREATE TABLE IF NOT EXISTS pending_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,     -- 追記順を保つ連番
        created_at TEXT NOT NULL,                         -- 記録時刻（表示用文字列）
        question   TEXT NOT NULL,                         -- ユーザーの発言
        response   TEXT NOT NULL                          -- ボットの応答
    )""",
]

# queue_tasks の列名一覧。UPDATE 文へ列名を埋め込む際のホワイトリストに使う。
_QUEUE_COLUMNS = (
    "task_id",
    "status",
    "channel_id",
    "message_id",
    "author_id",
    "question",
    "attachment_count",
    "created_at",
    "updated_at",
    "finished_at",
    "error",
)


def _json_loads_or(text: Any, default: Any) -> Any:
    """JSON 文字列を読み、壊れていたら default を返す（フェイルオープン）。"""
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


class Database:
    """memory/bot.db への読み書きを一手に引き受けるクラス。

    サービス各所が asyncio.to_thread 経由（＝別スレッド）で呼ぶため、
    check_same_thread=False で接続し、自前の threading.Lock で直列化する。
    書き込みは `with self._conn:` のトランザクションで原子的に行う
    （途中でクラッシュしてもファイルが壊れない — 旧 JSON 方式にはなかった利点）。
    """

    def __init__(self, db_path: str | Path, legacy_dir: str | Path | None = None) -> None:
        self._db_path = Path(db_path)
        # 旧 JSON ファイルの置き場所。省略時は DB と同じディレクトリ（memory/）。
        self._legacy_dir = Path(legacy_dir) if legacy_dir is not None else self._db_path.parent
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 行に列名でアクセスできるようにする
        # WAL モード: ボット稼働中でも sqlite3 CLI 等から安全に読み取れる
        self._conn.execute("PRAGMA journal_mode=WAL")

    def initialize(self) -> None:
        """テーブル作成 → スキーマ移行 → 旧ファイル取り込み → profile.json 吸収。起動時に1回呼ぶ。"""
        with self._lock, self._conn:
            for statement in _CREATE_TABLE_STATEMENTS:
                self._conn.execute(statement)
            self._apply_migrations()
        self._import_legacy_files()
        self._absorb_profile_json()

    def close(self) -> None:
        self._conn.close()

    # ---- スキーマ移行 -------------------------------------------------------

    def _apply_migrations(self) -> None:
        """PRAGMA user_version と SCHEMA_VERSION を比べ、未適用の移行だけを実行する。"""
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, []):
                self._conn.execute(statement)
            # user_version はプレースホルダ(?)が使えないため f-string で埋める。
            # version は range() 由来の int なのでインジェクションの心配はない。
            self._conn.execute(f"PRAGMA user_version = {version}")
            if _MIGRATIONS.get(version):
                print(f"データベース: スキーマをバージョン {version} へ移行しました。")

    # ---- 旧 JSON / テキストファイルの取り込み --------------------------------

    def _import_legacy_files(self) -> None:
        importers = [
            ("affinity.json", self._import_affinity_json),
            ("user_stats.json", self._import_user_stats_json),
            ("queue.json", self._import_queue_json),
            ("pending_log.txt", self._import_pending_log_txt),
        ]
        for filename, importer in importers:
            path = self._legacy_dir / filename
            if not path.exists():
                continue
            try:
                importer(path)
                backup_path = path.with_name(path.name + ".imported.bak")
                path.replace(backup_path)
                print(f"データベース: {filename} を取り込み、{backup_path.name} に改名しました。")
            except Exception as error:
                # 取り込み失敗でも起動は続ける（ファイルが残るので次回起動時に再試行される）
                print(f"データベース: {filename} の取り込みに失敗しました: {error}")

    def _import_affinity_json(self, path: Path) -> None:
        data = _json_loads_or(path.read_text(encoding="utf-8"), {})
        if not isinstance(data, dict):
            return
        with self._lock, self._conn:
            for user_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                # 既に DB にあるユーザーは上書きしない（DB 側を正とする）
                self._conn.execute(
                    "INSERT OR IGNORE INTO affinity"
                    " (user_id, affinity, valence, arousal, last_interaction)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        str(user_id),
                        entry.get("affinity", 50),
                        entry.get("valence", 50),
                        entry.get("arousal", 50),
                        entry.get("last_interaction", 0.0),
                    ),
                )

    def _import_user_stats_json(self, path: Path) -> None:
        data = _json_loads_or(path.read_text(encoding="utf-8"), {})
        if not isinstance(data, dict):
            return
        with self._lock, self._conn:
            for user_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                analysis = entry.get("analysis")
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_stats"
                    " (user_id, user_name, total_count, hourly, first_seen, last_seen,"
                    "  recent_samples, analysis)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(user_id),
                        str(entry.get("user_name", "")),
                        int(entry.get("total_count", 0)),
                        json.dumps(entry.get("hourly") or [0] * 24, ensure_ascii=False),
                        str(entry.get("first_seen", "")),
                        str(entry.get("last_seen", "")),
                        json.dumps(entry.get("recent_samples") or [], ensure_ascii=False),
                        json.dumps(analysis, ensure_ascii=False) if analysis else None,
                    ),
                )

    def _import_queue_json(self, path: Path) -> None:
        data = _json_loads_or(path.read_text(encoding="utf-8"), {})
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        if not isinstance(tasks, list):
            return
        with self._lock, self._conn:
            for task in tasks:
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO queue_tasks"
                    " (task_id, status, channel_id, message_id, author_id, question,"
                    "  attachment_count, created_at, updated_at, finished_at, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(task.get("task_id")),
                        str(task.get("status", "pending")),
                        int(task.get("channel_id", 0)),
                        int(task.get("message_id", 0)),
                        int(task.get("author_id", 0)),
                        str(task.get("question", "")),
                        int(task.get("attachment_count", 0)),
                        str(task.get("created_at", "")),
                        str(task.get("updated_at", "")),
                        task.get("finished_at"),
                        task.get("error"),
                    ),
                )

    def _import_pending_log_txt(self, path: Path) -> None:
        """旧 pending_log.txt（[時刻] User: … / [時刻] Bot: … 形式）を行単位で復元する。"""
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return
        entries: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        bot_seen = False
        for line in text.splitlines():
            user_match = re.match(r"^\[(.+?)\] User: (.*)$", line)
            bot_match = re.match(r"^\[(.+?)\] Bot: (.*)$", line)
            if user_match:
                if current is not None:
                    entries.append(current)
                current = {"created_at": user_match[1], "question": user_match[2], "response": ""}
                bot_seen = False
            elif bot_match and current is not None:
                current["response"] = bot_match[2]
                bot_seen = True
            elif current is not None:
                # 発言が複数行だった場合の続きの行。直前に書いていた側へ連結する。
                key = "response" if bot_seen else "question"
                current[key] += "\n" + line
        if current is not None:
            entries.append(current)

        with self._lock, self._conn:
            for entry in entries:
                self._conn.execute(
                    "INSERT INTO pending_log (created_at, question, response) VALUES (?, ?, ?)",
                    (entry["created_at"], entry["question"], entry["response"]),
                )

    def _absorb_profile_json(self) -> None:
        """profile.json に書き足された内容を profile テーブルへ吸収する（毎起動時）。

        中身があればトップレベルキー単位でマージ（同名キーはファイル側が勝つ）し、
        ファイルを {} に戻す。JSON として読めない場合はファイルを壊さず残し、
        警告だけ出してスキップする（手書きミスでデータを失わないため）。
        """
        path = self._legacy_dir / "profile.json"
        if not path.exists():
            return
        try:
            raw_text = path.read_text(encoding="utf-8").strip()
            if not raw_text or raw_text == "{}":
                return
            data = json.loads(raw_text)
            if not isinstance(data, dict) or not data:
                return
            self.profile_merge(data)
            path.write_text("{}", encoding="utf-8")
            print(f"データベース: profile.json の内容 {list(data.keys())} を profile テーブルへ追加しました。")
        except json.JSONDecodeError as error:
            print(f"データベース: profile.json が JSON として読めないため取り込みをスキップします: {error}")
        except Exception as error:
            print(f"データベース: profile.json の取り込みに失敗しました: {error}")

    # ---- profile（プロフィール記憶） ------------------------------------------

    def profile_load(self) -> dict[str, Any]:
        """プロフィール記憶全体を {キー: 値} 形式で返す（旧 profile.json と同じ形）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM profile ORDER BY key").fetchall()
        return {row["key"]: _json_loads_or(row["value"], None) for row in rows}

    def profile_merge(self, updates: dict[str, Any]) -> None:
        """トップレベルキー単位でマージする（同名キーは updates 側で上書き）。"""
        with self._lock, self._conn:
            for key, value in updates.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO profile (key, value) VALUES (?, ?)",
                    (str(key), json.dumps(value, ensure_ascii=False)),
                )

    def profile_replace_all(self, profile: dict[str, Any]) -> None:
        """プロフィール全体を置き換える（記憶圧縮の結果を保存する際に使う）。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM profile")
            for key, value in profile.items():
                self._conn.execute(
                    "INSERT INTO profile (key, value) VALUES (?, ?)",
                    (str(key), json.dumps(value, ensure_ascii=False)),
                )

    # ---- affinity（親密度・感情） --------------------------------------------

    def affinity_load_all(self) -> dict[str, dict[str, Any]]:
        """全ユーザーの親密度データを {user_id: {...}} 形式で返す（旧 JSON と同じ形）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM affinity").fetchall()
        return {
            row["user_id"]: {
                "affinity": row["affinity"],
                "valence": row["valence"],
                "arousal": row["arousal"],
                "last_interaction": row["last_interaction"],
            }
            for row in rows
        }

    def affinity_save_all(self, cache: dict[str, dict[str, Any]]) -> None:
        """キャッシュ全体で置き換える（旧「ファイル全体を書き直す」動作と等価）。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM affinity")
            for user_id, entry in cache.items():
                self._conn.execute(
                    "INSERT INTO affinity"
                    " (user_id, affinity, valence, arousal, last_interaction)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        str(user_id),
                        entry.get("affinity", 50),
                        entry.get("valence", 50),
                        entry.get("arousal", 50),
                        entry.get("last_interaction", 0.0),
                    ),
                )

    # ---- user_stats（会話統計） ----------------------------------------------

    def stats_load_all(self) -> dict[str, dict[str, Any]]:
        """全ユーザーの統計を {user_id: {...}} 形式で返す（旧 JSON と同じ形）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM user_stats").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[row["user_id"]] = {
                "user_name": row["user_name"],
                "total_count": row["total_count"],
                "hourly": _json_loads_or(row["hourly"], [0] * 24),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "recent_samples": _json_loads_or(row["recent_samples"], []),
                "analysis": _json_loads_or(row["analysis"], None),
            }
        return result

    def stats_save_all(self, cache: dict[str, dict[str, Any]]) -> None:
        """キャッシュ全体で置き換える（旧「ファイル全体を書き直す」動作と等価）。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM user_stats")
            for user_id, entry in cache.items():
                analysis = entry.get("analysis")
                self._conn.execute(
                    "INSERT INTO user_stats"
                    " (user_id, user_name, total_count, hourly, first_seen, last_seen,"
                    "  recent_samples, analysis)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(user_id),
                        str(entry.get("user_name", "")),
                        int(entry.get("total_count", 0)),
                        json.dumps(entry.get("hourly") or [0] * 24, ensure_ascii=False),
                        str(entry.get("first_seen", "")),
                        str(entry.get("last_seen", "")),
                        json.dumps(entry.get("recent_samples") or [], ensure_ascii=False),
                        json.dumps(analysis, ensure_ascii=False) if analysis else None,
                    ),
                )

    # ---- queue_tasks（文書処理キュー） ----------------------------------------

    def queue_upsert_task(self, task: dict[str, Any]) -> None:
        """タスクを追加する（同じ task_id があれば丸ごと置き換える）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO queue_tasks"
                " (task_id, status, channel_id, message_id, author_id, question,"
                "  attachment_count, created_at, updated_at, finished_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task.get("task_id", "")),
                    str(task.get("status", "pending")),
                    int(task.get("channel_id", 0)),
                    int(task.get("message_id", 0)),
                    int(task.get("author_id", 0)),
                    str(task.get("question", "")),
                    int(task.get("attachment_count", 0)),
                    str(task.get("created_at", "")),
                    str(task.get("updated_at", "")),
                    task.get("finished_at"),
                    task.get("error"),
                ),
            )

    def queue_update_task(self, task_id: str, updates: dict[str, Any], updated_at: str) -> None:
        """指定タスクの列を部分更新する（updated_at は常に更新される）。"""
        # 列名は SQL 文へ直接埋め込むため、既知の列だけを許可する（インジェクション対策）
        safe_updates = {
            key: value
            for key, value in updates.items()
            if key in _QUEUE_COLUMNS and key != "task_id"
        }
        safe_updates["updated_at"] = updated_at
        set_clause = ", ".join(f"{key} = ?" for key in safe_updates)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE queue_tasks SET {set_clause} WHERE task_id = ?",
                (*safe_updates.values(), str(task_id)),
            )

    def queue_claim_next_pending(self, now_iso: str) -> dict[str, Any] | None:
        """最も古い pending タスクを processing に変えて返す。無ければ None。

        SELECT と UPDATE を同一トランザクション内で行うため、
        同じタスクが二重に取得されることはない。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM queue_tasks WHERE status = 'pending'"
                " ORDER BY created_at, task_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE queue_tasks SET status = 'processing', updated_at = ? WHERE task_id = ?",
                (now_iso, row["task_id"]),
            )
            task = dict(row)
            task["status"] = "processing"
            task["updated_at"] = now_iso
            return task

    def queue_recover_stuck(self, now_iso: str) -> int:
        """processing のまま残ったタスクを pending に戻し、戻した件数を返す。"""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE queue_tasks SET status = 'pending', updated_at = ?"
                " WHERE status = 'processing'",
                (now_iso,),
            )
            return cursor.rowcount

    def queue_prune_finished(self, keep_count: int) -> None:
        """完了/失敗タスクを新しい順に keep_count 件だけ残し、それ以前を削除する。"""
        with self._lock, self._conn:
            self._conn.execute(
                """DELETE FROM queue_tasks
                   WHERE status IN ('completed', 'failed')
                     AND task_id NOT IN (
                         SELECT task_id FROM queue_tasks
                         WHERE status IN ('completed', 'failed')
                         ORDER BY created_at DESC, task_id DESC
                         LIMIT ?
                     )""",
                (int(keep_count),),
            )

    # ---- pending_log（記憶圧縮前の一時ログ） ----------------------------------

    def pending_append(self, created_at: str, question: str, response: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pending_log (created_at, question, response) VALUES (?, ?, ?)",
                (created_at, question, response),
            )

    def pending_fetch_entries(self) -> list[dict[str, Any]]:
        """一時ログの全件を追記順で返す。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM pending_log ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def pending_clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM pending_log")
