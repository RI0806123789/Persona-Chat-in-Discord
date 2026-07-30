"""fire-and-forget なバックグラウンドタスクの起動をまとめたヘルパー。

asyncio のイベントループは実行中のタスクを**弱参照**でしか保持しない。そのため
`asyncio.create_task(...)` の戻り値をどこにも保存しないと、そのタスクは
実行の途中でガベージコレクトされて消えることがある（Python 公式ドキュメントが
「タスクが実行途中で消えるのを避けるため、戻り値への参照を保持すること」と
明記している既知の落とし穴。運が良ければ "Task was destroyed but it is pending!"
という警告が出るが、出ないまま静かに消えることもある）。

このモジュールの `spawn()` は生成したタスクへの強参照をモジュール内の集合で保持し、
完了時に自動で取り除く。加えて、タスク内で発生した例外をその場でログに出す
（`create_task` に投げっぱなしにすると、例外はタスクが回収されるまで報告されず、
最悪の場合まったく表示されないまま失われる）。

**fire-and-forget でコルーチンを起動するときは必ず `spawn()` を使うこと。**
`asyncio.create_task()` を直接呼ばないこと。
"""

import asyncio
from typing import Any, Coroutine

# 実行中タスクへの強参照。完了時にコールバックで取り除くため溜まり続けることはない。
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, label: str) -> asyncio.Task[Any]:
    """コルーチンをバックグラウンドで実行する。完了までタスクへの強参照を保つ。

    Args:
        coro: 実行するコルーチン。
        label: 失敗時のログに出す処理名（例: "会話統計の記録"）。
            どの処理が落ちたのか追えるよう、呼び出し側で必ず指定すること。
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(lambda finished: _on_task_done(finished, label))
    return task


def _on_task_done(task: asyncio.Task[Any], label: str) -> None:
    """タスク完了時に強参照を外し、例外があればログに出す。"""
    _background_tasks.discard(task)

    if task.cancelled():
        # ボット停止時などの正常なキャンセル。エラーとして騒がない。
        return

    error = task.exception()
    if error is not None:
        print(f"バックグラウンド処理エラー ({label}): {type(error).__name__}: {error}")


def pending_count() -> int:
    """実行中のバックグラウンドタスク数（デバッグ・動作確認用）。"""
    return len(_background_tasks)
