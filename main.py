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

from services.storage.database import init_database  # noqa: E402

init_database()  # news.db (含 Phase -1 一次性 DROP 旧 AI 表)

from services.market import MarketDB  # noqa: E402
from services.storage.ai_inference_db import (  # noqa: E402
    get_ai_inference_db,
)


def _init_market_db_on_startup() -> None:
    """初始化 ``data/market.db`` 并跑迁移（首次运行会建 14 张表 + 30+ 游资种子）。

    自动备份逻辑：若库已存在且有未应用迁移，迁移前会复制一份到
    ``data/backups/``。任何失败只会写到 stderr，不阻断 GUI 启动。
    """
    try:
        db = MarketDB()
        applied = db.ensure_schema()
        if applied:
            print(
                "[启动] market.db 迁移完成: "
                + ", ".join(str(v) for v in applied),
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[启动] market.db 初始化失败: {exc}", file=sys.stderr)


def _init_ai_inference_db_on_startup() -> None:
    """初始化 ``data/ai_inference.db``（Phase -1 三库重构新增）。

    存放所有 AI 衍生数据（ai_reports / theme_* / *_scores），
    迁移失败仅写 stderr 不阻断启动。
    """
    try:
        applied = get_ai_inference_db().ensure_schema()
        if applied:
            print(
                "[启动] ai_inference.db 迁移完成: "
                + ", ".join(str(v) for v in applied),
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[启动] ai_inference.db 初始化失败: {exc}", file=sys.stderr)


_init_market_db_on_startup()
_init_ai_inference_db_on_startup()


def _check_three_db_health_on_startup() -> None:
    """Phase 0 卫兵：三库 schema 健康检查（不阻断启动）。

    各库的 ensure_schema() 跑完后再做一次"骨架表 + 跨库 ATTACH + PRAGMA"
    巡检；任何 FAIL 项写到 stderr 提示主人，但 GUI 仍正常启动。
    """
    try:
        from tools.check_three_db_health import run_all_checks
        report = run_all_checks()
        if report.fail_count == 0:
            return
        print(
            f"[启动] 三库健康检查发现 {report.fail_count} 项 FAIL（"
            f"不阻断 GUI 启动，建议跑 python tools/check_three_db_health.py 排查）:",
            file=sys.stderr,
        )
        for item in report.items:
            if item.status == "FAIL":
                print(f"  - [{item.name}] {item.detail}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[启动] 三库健康检查异常: {exc}", file=sys.stderr)


_check_three_db_health_on_startup()

from core.prompt_loader import PromptLoader  # noqa: E402


def _validate_prompts_on_startup() -> None:
    """启动校验所有 prompts/ 文件。

    发现错误时打印到 stderr 并写到 .huiye/_prompt_validation.txt 便于排查，
    但不阻断 GUI 启动——prompt 故障下其它功能（爬虫/清洗）仍可用。
    """
    try:
        loader = PromptLoader()
        errors = loader.validate_all()
    except Exception as e:  # noqa: BLE001
        print(f"[启动校验] PromptLoader 初始化失败: {e}", file=sys.stderr)
        return
    if not errors:
        return
    print(
        f"[启动校验] 发现 {len(errors)} 个 prompt 问题（不阻断启动）:",
        file=sys.stderr,
    )
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    try:
        log_path = os.path.join(_ROOT, ".huiye", "_prompt_validation.txt")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            for e in errors:
                f.write(e + "\n")
    except Exception:
        pass


_validate_prompts_on_startup()

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
