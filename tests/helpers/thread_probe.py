"""同期処理の待機中にもイベントループが進むことを確認する補助。"""

import asyncio
import threading


def make_thread_probe(result):
    """ループから再開を許可する同期関数と、その実行スレッドの記録を返す。"""
    loop = asyncio.get_running_loop()
    threads = []

    def operation(*args, **kwargs):
        threads.append(threading.get_ident())
        released = threading.Event()
        loop.call_soon_threadsafe(released.set)
        if not released.wait(2):
            raise AssertionError("同期処理の待機中にイベントループが進みません。")
        return result

    return operation, threads
