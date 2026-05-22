"""
新闻爬虫管理系统 - PyQt5 桌面版
主入口文件
"""

import sys
import os
import platform

# 添加项目根目录到路径
_ROOT = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, _ROOT)

from core.env_loader import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_ROOT, ".env"))

from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtGui import QFont, QIcon  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402


def _set_windows_appusermodel_id(app_id: str) -> None:
    """
    Windows 任务栏分组/图标相关：设置当前进程的 AppUserModelID。
    仅在 Windows 下生效；失败不影响程序运行。
    """
    if platform.system().lower() != "windows":
        return
    try:
        import ctypes  # noqa: WPS433 (std lib)

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def main():
    """程序主入口"""
    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName("新闻爬虫管理系统")
    app.setOrganizationName("NewsBot")

    # 任务栏/窗口图标（窗口自身也会再 setWindowIcon，这里设置全局默认更稳）
    icon_path = os.path.join(
        os.path.abspath(os.path.dirname(__file__)),
        "图标.ico",
    )
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Windows 任务栏分组/图标稳定性（建议保持不变）
    _set_windows_appusermodel_id("NewsBot.NewsCrawlerSystem")

    # 设置全局字体
    font = QFont("微软雅黑", 10)
    app.setFont(font)

    # 创建启动画面（可选）
    # splash = QSplashScreen()
    # splash.show()
    # app.processEvents()

    # 创建主窗口
    window = MainWindow()
    window.show()

    # 如果有启动画面，延迟关闭
    # QTimer.singleShot(1000, splash.close)

    # 运行应用
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
