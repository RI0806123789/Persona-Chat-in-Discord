"""全グラフの日本語フォント設定とmatplotlib操作の直列化を共有する。"""

import io
from functools import wraps
import threading

from matplotlib import font_manager, rcParams

from persona_bot.config.constants import GRAPH_FONT_FAMILY


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


def save_figure_to_buffer(figure, **savefig_options) -> io.BytesIO:
    """PNGの所有権を呼出元へ渡し、失敗時のバッファとFigureを回収する。"""
    from matplotlib import pyplot as plt

    buffer = io.BytesIO()
    try:
        figure.savefig(buffer, format="png", **savefig_options)
        buffer.seek(0)
        return buffer
    except BaseException:
        buffer.close()
        raise
    finally:
        plt.close(figure)
