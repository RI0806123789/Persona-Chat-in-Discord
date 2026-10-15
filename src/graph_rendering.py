"""全グラフのmatplotlib操作を直列化する。描画はワーカースレッドから呼ぶ。"""

from functools import wraps
import threading


_PLOT_LOCK = threading.Lock()


def serialized_plot(function):
    @wraps(function)
    def render(*args, **kwargs):
        # matplotlibの共有設定・Figure管理を、統計と利用量の間でも保護する。
        with _PLOT_LOCK:
            return function(*args, **kwargs)
    return render
