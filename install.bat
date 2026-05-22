@echo off
chcp 65001
cls

echo ========================================
echo 安装 PyQt5 项目依赖
echo ========================================
echo.

echo 正在安装 Python 依赖包...
pip install -r requirements.txt

echo.
echo ========================================
echo 安装完成！
echo ========================================
echo.
echo 使用说明：
echo 1. 双击 start.bat 启动程序
echo 2. 或使用命令: python main.py
echo.

pause







