# 🎁 PyQt5 版本打包说明

## 📦 打包成独立可执行文件

### 方法1: 使用 PyInstaller（推荐）

#### 1. 安装 PyInstaller

```bash
pip install pyinstaller
```

#### 2. 打包

```bash
pyinstaller build.spec
```

或者使用命令行：

```bash
pyinstaller --name="新闻爬虫系统" ^
    --windowed ^
    --add-data="core;core" ^
    --add-data="gui;gui" ^
    --hidden-import="PyQt5" ^
    main.py
```

#### 3. 查找打包结果

打包完成后，在 `dist` 目录下会生成可执行文件。

---

### 方法2: 使用 Auto-py-to-exe（图形界面）

#### 1. 安装

```bash
pip install auto-py-to-exe
```

#### 2. 启动

```bash
auto-py-to-exe
```

#### 3. 配置

- **Script Location**: 选择 `main.py`
- **Onefile**: 选择 "One Directory"
- **Console Window**: 选择 "Window Based"
- **Additional Files**: 添加 `core` 和 `gui` 目录
- **Icon**: 如果有图标文件，可以添加

#### 4. 点击 "CONVERT .PY TO .EXE" 开始打包

---

## 📋 打包注意事项

### 1. 依赖检查

确保所有依赖都已安装：

```bash
pip install -r requirements_pyqt.txt
```

### 2. 测试运行

打包前先测试程序是否能正常运行：

```bash
python main.py
```

### 3. ChromeDriver 处理

- ChromeDriver 需要单独放在 exe 旁边
- 或者在代码中指定 ChromeDriver 路径

### 4. 数据目录

打包后需要确保以下目录存在：
- data/raw
- data/summaries
- data/analysis
- logs

可以在程序启动时自动创建这些目录。

---

## 🚀 分发

### 1. 打包成压缩文件

```
新闻爬虫系统_v1.0.zip
├── 新闻爬虫系统.exe
├── chrome-win64/         # ChromeDriver
├── data/                 # 数据目录（空）
├── logs/                 # 日志目录（空）
├── 使用说明.txt
└── _internal/            # PyInstaller生成的依赖文件
```

### 2. 制作安装程序（可选）

使用 Inno Setup 或 NSIS 制作安装程序。

---

## 💡 优化建议

### 1. 减小体积

```bash
pyinstaller --onefile --windowed main.py
```

但这会增加启动时间。

### 2. 排除不需要的模块

在 build.spec 中添加：

```python
excludes=['tkinter', 'matplotlib', ...]
```

### 3. 使用 UPX 压缩

```bash
pyinstaller --upx-dir=/path/to/upx build.spec
```

---

## 🐛 常见问题

### 1. 打包后无法启动

- 检查是否有路径问题
- 使用 `--console` 模式查看错误信息

### 2. 缺少模块

在 build.spec 中添加 `hiddenimports`：

```python
hiddenimports=['PyQt5.QtPrintSupport', ...]
```

### 3. 文件路径问题

使用相对路径时，需要处理打包后的路径：

```python
import sys
import os

if getattr(sys, 'frozen', False):
    # 打包后的路径
    base_path = sys._MEIPASS
else:
    # 开发时的路径
    base_path = os.path.dirname(__file__)
```

---

## ✅ 打包清单

- [ ] 测试所有功能正常
- [ ] 安装所有依赖
- [ ] 准备好 ChromeDriver
- [ ] 运行 PyInstaller
- [ ] 测试打包后的程序
- [ ] 准备使用说明
- [ ] 打包成压缩文件
- [ ] 测试在干净的系统上运行

---

**打包完成后，就可以分发给其他用户使用了！** 🎉

