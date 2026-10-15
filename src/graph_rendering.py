"""全グラフの日本語フォント設定とmatplotlib操作の直列化を共有する。"""

from functools import wraps
import threading

from matplotlib import font_manager, rcParams

from constants import GRAPH_FONT_FAMILY


_PLOT_LOCK = threading.Lock()


def graph_font_context() -> dict[str, list[str]]:
    """共通候補の順序を保ち、未導入フォントによる警告を避ける。"""
    installed = {font.name for font in font_manager.fontManager.ttflist}
    families = [family for family in GRAPH_FONT_FAMILY
                if family in installed or f"font.{family}" in rcParams]
    # 日本語フォント未導入でも描画自体は継続する。字形の準備はREADMEの手順で行う。
    return {"font.family": families or ["sans-serif"]}


def serialized_plot(function):
    @wraps(function)
    def render(*args, **kwargs):
        # matplotlibの共有設定・Figure管理を、統計と利用量の間でも保護する。
        with _PLOT_LOCK:
            return function(*args, **kwargs)
    return render
