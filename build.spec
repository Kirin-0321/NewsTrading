# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置文件

import sys
import os
from pathlib import Path

block_cipher = None

# 修复：使用 SPECPATH 代替 __file__
ROOT_DIR = Path(SPECPATH).resolve()
ICON_PATH = ROOT_DIR / "图标.ico"

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('core', 'core'),
        ('gui', 'gui'),
        ('config', 'config'),
        ('doc', 'doc'),
        ('Typora', 'Typora'),  # 打包Typora (Markdown阅读器)
        ('data', 'data'),  # 打包data目录
        ('workflows', 'workflows'),  # 打包workflows目录
        ('模板', '模板'),  # 打包提示词模板
        ('图标.ico', '.'),  # 打包程序图标
        ('盘后总结', '盘后总结'),  # 打包盘后总结示例
    ],
    hiddenimports=[
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'selenium',
        'selenium.webdriver',
        'selenium.webdriver.chrome',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.common.by',
        'schedule',
        'bs4',
        'requests',
        'openai',
        'zhipuai',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6',           # 排除PySide6，避免与PyQt5冲突
        'PySide2',           # 排除PySide2
        'tkinter',           # 排除tkinter
        'matplotlib',        # 排除matplotlib
        'numpy',             # 排除numpy（如果不使用）
        'pandas',            # 排除pandas
        'PIL',               # 排除PIL
        'scipy',             # 排除scipy
        'wx',                # 排除wxPython
        'PyQt6',             # 排除PyQt6
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='新闻爬虫系统',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 显示控制台窗口，便于查看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,  # exe/任务栏图标
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='新闻爬虫系统',
)

