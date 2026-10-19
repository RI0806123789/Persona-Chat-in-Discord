"""専用ランチャーの標準入力パイプを、uvicornへの正常終了要求として扱う。"""

import logging
import sys
import threading
import time

import uvicorn


MANAGED_ENV = "PERSONA_CONSOLE_MANAGED"
STARTED_POLL_INTERVAL = 0.05


def run_server(app, *, host: str, port: int, managed: bool = False) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    if managed:
        def watch_launcher() -> None:
            # 親だけが持つ書込端が閉じればEOFになる。HTTPの終了APIは設けない。
            try:
                sys.stdin.buffer.read(1)
            except OSError:
                logging.exception("ランチャーの終了通知を読めませんでした。正常終了へ進みます。")
            # 起動中に終了要求が届いた場合も、lifespan開始後のshutdownへ接続する。
            while not server.started and not server.should_exit:
                time.sleep(STARTED_POLL_INTERVAL)
            logging.info("ランチャーの終了要求を受け取り、進行中の処理の終了を待ちます。")
            server.should_exit = True

        threading.Thread(target=watch_launcher, name="console-shutdown", daemon=True).start()
    server.run()
