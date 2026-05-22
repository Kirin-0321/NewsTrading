"""
样式表定义
"""

# 主窗口样式
MAIN_WINDOW_STYLE = """
QMainWindow {
    background-color: #f0f2f5;
}
"""

# 侧边栏样式
SIDEBAR_STYLE = """
QWidget {
    background-color: #001529;
}

QPushButton {
    background-color: transparent;
    color: white;
    text-align: left;
    padding: 15px 20px;
    border: none;
    font-size: 14px;
}

QPushButton:hover {
    background-color: rgba(24, 144, 255, 0.2);
}

QPushButton:checked {
    background-color: #1890ff;
}

QLabel {
    color: white;
    font-size: 18px;
    font-weight: bold;
    padding: 20px;
}
"""

# 内容区域样式
CONTENT_STYLE = """
QWidget {
    background-color: white;
    border-radius: 4px;
}
"""

# 按钮样式
BUTTON_STYLE = """
QPushButton {
    background-color: white;
    color: #262626;
    border: 1px solid #d9d9d9;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 14px;
}

QPushButton:hover {
    color: #1890ff;
    border-color: #1890ff;
}

QPushButton:pressed {
    color: #096dd9;
    border-color: #096dd9;
}

QPushButton:disabled {
    background-color: #f5f5f5;
    color: #8c8c8c;
    border-color: #d9d9d9;
}
"""

BUTTON_PRIMARY = """
QPushButton {
    background-color: #1890ff;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 14px;
}

QPushButton:hover {
    background-color: #40a9ff;
}

QPushButton:pressed {
    background-color: #096dd9;
}

QPushButton:disabled {
    background-color: #d9d9d9;
    color: #8c8c8c;
}
"""

BUTTON_SUCCESS = """
QPushButton {
    background-color: #52c41a;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 14px;
}

QPushButton:hover {
    background-color: #73d13d;
}
"""

BUTTON_DANGER = """
QPushButton {
    background-color: #ff4d4f;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 14px;
}

QPushButton:hover {
    background-color: #ff7875;
}
"""

# 表格样式
TABLE_STYLE = """
QTableWidget {
    border: 1px solid #f0f0f0;
    gridline-color: #f0f0f0;
    selection-background-color: #e6f7ff;
}

QTableWidget::item {
    padding: 5px;
}

QHeaderView::section {
    background-color: #fafafa;
    padding: 8px;
    border: 1px solid #f0f0f0;
    font-weight: bold;
}
"""

# 进度条样式
PROGRESS_STYLE = """
QProgressBar {
    border: 1px solid #d9d9d9;
    border-radius: 4px;
    text-align: center;
    background-color: #f5f5f5;
}

QProgressBar::chunk {
    background-color: #1890ff;
    border-radius: 3px;
}
"""

# 文本框样式
TEXTBROWSER_STYLE = """
QTextBrowser {
    border: 1px solid #d9d9d9;
    border-radius: 4px;
    padding: 8px;
    background-color: white;
}
"""

# 组合框样式
COMBOBOX_STYLE = """
QComboBox {
    border: 1px solid #d9d9d9;
    border-radius: 4px;
    padding: 5px;
    background-color: white;
}

QComboBox:hover {
    border-color: #40a9ff;
}

QComboBox::drop-down {
    border: none;
}
"""

# 输入框样式
INPUT_STYLE = """
QLineEdit, QSpinBox {
    border: 1px solid #d9d9d9;
    border-radius: 4px;
    padding: 5px 10px;
    background-color: white;
}

QLineEdit:focus, QSpinBox:focus {
    border-color: #40a9ff;
}
"""

