@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ========================================
:: 新闻爬虫系统 - 一键打包工具
:: ========================================

cls
echo.
echo ========================================
echo    新闻爬虫系统 - 一键打包工具
echo ========================================
echo.

:: 检查Python环境
echo [1/6] 正在检查Python环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python环境，请先安装Python 3.7+
    pause
    exit /b 1
)
python --version
echo.

:: 检查PyInstaller
echo [2/6] 正在检查PyInstaller...
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [提示] PyInstaller未安装，正在安装...
    pip install pyinstaller>=6.0.0
    if errorlevel 1 (
        echo [错误] PyInstaller安装失败
        pause
        exit /b 1
    )
)
echo [完成] PyInstaller已就绪
echo.

:: 清理旧的构建文件
echo [3/6] 正在清理旧的构建文件...
if exist "build" (
    echo     - 删除 build 目录
    rmdir /s /q "build"
)
if exist "dist" (
    echo     - 删除 dist 目录
    rmdir /s /q "dist"
)
echo [完成] 清理完成
echo.

:: 开始打包
echo [4/6] 正在执行PyInstaller打包...
echo ----------------------------------------
echo 打包配置: build.spec
echo 目标名称: 新闻爬虫系统
echo 打包模式: 目录模式 (非单文件)
echo ----------------------------------------
echo.
pyinstaller build.spec --clean
if errorlevel 1 (
    echo.
    echo [错误] 打包失败！请检查上面的错误信息
    pause
    exit /b 1
)
echo.

:: 复制Chrome浏览器
echo [5/6] 正在复制Chrome浏览器...
if exist "chrome-win64" (
    if not exist "dist\新闻爬虫系统\chrome-win64" (
        echo     - 正在复制 chrome-win64 目录...
        xcopy /E /I /Y "chrome-win64" "dist\新闻爬虫系统\chrome-win64" >nul
        echo     [完成] Chrome浏览器复制成功
    ) else (
        echo     [跳过] Chrome浏览器已存在
    )
) else (
    echo     [警告] 未找到chrome-win64目录
)
echo.

:: 复制配置说明文件
echo [6/7] 正在复制配置说明文件...
if exist "打包后配置说明.md" (
    copy /Y "打包后配置说明.md" "dist\新闻爬虫系统\" >nul
    echo     [完成] 配置说明文件已复制
) else (
    echo     [跳过] 配置说明文件不存在
)
echo.

:: 检查和创建必要的目录
echo [7/7] 正在检查目录结构...
cd /d "dist\新闻爬虫系统"

if not exist "data" (
    echo     [提示] data目录未打包，正在创建...
    mkdir "data"
    mkdir "data\raw"
    mkdir "data\cleaned"
    mkdir "data\exports"
    mkdir "data\AI_analysis"
    mkdir "data\summaries"
    mkdir "data\analysis"
) else (
    echo     [完成] data目录已打包
)

if not exist "logs" (
    mkdir "logs"
    mkdir "logs\workflows"
)

echo     [完成] 目录结构检查完成
cd /d "%~dp0"
echo.

:: 统计打包大小
echo ========================================
echo 打包完成！
echo ========================================
echo.
echo 输出目录: dist\新闻爬虫系统\
echo.

:: 计算目录大小
for /f "tokens=3" %%a in ('dir /s "dist\新闻爬虫系统" ^| find "个文件"') do set size=%%a
echo 打包大小: %size% 字节
echo.

:: 显示重要文件
echo 打包内容清单:
echo   - 新闻爬虫系统.exe      (主程序)
echo   - chrome-win64\         (Chrome浏览器)
echo   - Typora\               (Markdown阅读器)
echo   - config\               (AI配置、清洗规则)
echo   - data\                 (数据目录)
echo   - workflows\            (工作流配置)
echo   - 模板\                 (提示词模板)
echo   - 盘后总结\             (盘后总结示例)
echo   - 图标.ico              (程序图标)
echo   - logs\                 (日志目录)
echo   - doc\                  (文档目录)
echo.

:: 询问是否打开目录
echo ========================================
echo.
set /p open="是否打开输出目录? (Y/N): "
if /i "%open%"=="Y" (
    explorer "dist\新闻爬虫系统"
)

echo.
echo ========================================
echo 重要提示
echo ========================================
echo.
echo [!] 打包完成后请注意:
echo.
echo 1. 首次运行前必须配置 API Key
echo    文件位置: config\ai_config.json
echo.
echo 2. 详细配置说明请查看:
echo    打包后配置说明.md
echo.
echo 3. 确保 chrome-win64 目录完整
echo    (约300MB, Chrome浏览器)
echo.
echo 4. 可以将整个目录复制到其他电脑使用
echo    (无需安装Python和依赖)
echo.
pause

