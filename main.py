#!/usr/bin/env python3
"""基于 PyQt-Fluent-Widgets 的 M系镜像主题下载器前端。"""

from __future__ import annotations

import html
import base64
import json
import sys
import time
from pathlib import Path

from PyQt5.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QSize,
    QThread,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
    QTextCursor,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ColorPickerButton,
    ComboBox,
    DoubleSpinBox,
    FluentIcon as FIF,
    FluentWindow,
    IndeterminateProgressBar,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    NavigationItemPosition,
    PrimaryPushButton,
    PushButton,
    RadioButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TextEdit,
    Theme,
    TitleLabel,
    setTheme,
    setThemeColor,
)

from download_thread import (
    Author,
    AuthorProfileData,
    ThreadData,
    ThreadDownloader,
    export_thread,
    normalize_url,
    render_post_body_html,
)


class FetchWorker(QThread):
    """完整主题抓取线程。"""

    dataReady = pyqtSignal(int, object, object)
    failed = pyqtSignal(int, str)
    progressChanged = pyqtSignal(int, int, int)
    logCreated = pyqtSignal(str, str, str)

    def __init__(self, generation: int, url: str, delay: float, timeout: int, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.url = url
        self.delay = delay
        self.timeout = timeout

    def _log(self, level: str, message: str) -> None:
        self.logCreated.emit(level, time.strftime("%H:%M:%S"), message)

    def run(self) -> None:
        try:
            downloader = ThreadDownloader(self.timeout, logger=self._log)
            data = downloader.fetch(
                self.url,
                page_delay=self.delay,
                progress=lambda page, posts: self.progressChanged.emit(
                    self.generation, page, posts
                ),
            )
            self.dataReady.emit(self.generation, data, downloader)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class AvatarWorker(QThread):
    """头像下载线程，不阻塞主题列表显示。"""

    avatarReady = pyqtSignal(int, str, bytes)
    allDone = pyqtSignal(int)
    logCreated = pyqtSignal(str, str, str)

    def __init__(
        self,
        generation: int,
        downloader: ThreadDownloader,
        authors: list[Author],
        parent=None,
    ):
        super().__init__(parent)
        self.generation = generation
        self.downloader = downloader
        self.authors = authors

    def _log(self, level: str, message: str) -> None:
        self.logCreated.emit(level, time.strftime("%H:%M:%S"), message)

    def run(self) -> None:
        self.downloader.logger = self._log
        for author in self.authors:
            if self.isInterruptionRequested():
                break
            if not author.avatar_url:
                continue
            try:
                raw = self.downloader.avatar_bytes(author.avatar_url)
                if raw:
                    self.avatarReady.emit(self.generation, author.key, raw)
            except Exception:
                # 详细原因已由后端按 WARNING 级别发送。
                pass
        self.allDone.emit(self.generation)


class ProfileWorker(QThread):
    profileReady = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)
    progressChanged = pyqtSignal(int, int, int)
    logCreated = pyqtSignal(str, str, str)

    def __init__(self, generation: int, author: Author, delay: float, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.author = author
        self.delay = delay

    def _log(self, level: str, message: str) -> None:
        self.logCreated.emit(level, time.strftime("%H:%M:%S"), message)

    def run(self) -> None:
        try:
            downloader = ThreadDownloader(20, logger=self._log)
            profile = downloader.fetch_author_profile(
                self.author.name,
                self.author.profile_url,
                self.author.avatar_url,
                page_delay=self.delay,
                progress=lambda page, works: self.progressChanged.emit(
                    self.generation, page, works
                ),
            )
            self.profileReady.emit(self.generation, profile)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class ViewerThreadWorker(QThread):
    dataReady = pyqtSignal(int, object, object)
    failed = pyqtSignal(int, str)
    logCreated = pyqtSignal(str, str, str)

    def __init__(self, generation: int, thread_url: str, delay: float, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.thread_url = thread_url
        self.delay = delay

    def _log(self, level: str, message: str) -> None:
        self.logCreated.emit(level, time.strftime("%H:%M:%S"), message)

    def run(self) -> None:
        try:
            downloader = ThreadDownloader(20, logger=self._log)
            data = downloader.fetch(
                self.thread_url, page_delay=self.delay
            )
            avatars: dict[str, bytes] = {}
            for author in data.authors():
                if author.avatar_url:
                    try:
                        raw = downloader.avatar_bytes(author.avatar_url)
                        if raw:
                            avatars[author.key] = raw
                    except Exception:
                        pass
            self.dataReady.emit(self.generation, data, avatars)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class SelectAllHeader(QHeaderView):
    """在表格左上角放置圆形全选控件。"""

    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.selectButton = RadioButton("", self)
        self.selectButton.setAutoExclusive(False)
        self.selectButton.setFixedSize(24, 24)
        self.selectButton.toggled.connect(self.toggled.emit)
        self.sectionResized.connect(lambda *_: self._position_button())
        self.geometriesChanged.connect(self._position_button)

    def _position_button(self) -> None:
        if self.count() == 0:
            return
        x = self.sectionViewportPosition(0) + (
            self.sectionSize(0) - self.selectButton.width()
        ) // 2
        y = (self.height() - self.selectButton.height()) // 2
        self.selectButton.move(x, y)
        self.selectButton.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_button()


class DownloadInterface(QWidget):
    """下载与作者筛选页面，仅负责界面呈现。"""

    fetchRequested = pyqtSignal()
    browseRequested = pyqtSignal()
    exportRequested = pyqtSignal()
    profileRequested = pyqtSignal(int)
    authorSelectionChanged = pyqtSignal()
    selectAllChanged = pyqtSignal(bool)
    viewThreadRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadInterface")
        self.hasData = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        title = SubtitleLabel("单主题下载器", self)
        root.addWidget(title)

        query_card = CardWidget(self)
        query_layout = QVBoxLayout(query_card)
        query_layout.setContentsMargins(20, 16, 20, 18)
        query_layout.setSpacing(10)
        query_layout.addWidget(StrongBodyLabel("主题链接或 ID", query_card))

        query_row = QHBoxLayout()
        query_row.setSpacing(10)
        self.urlEdit = LineEdit(query_card)
        self.urlEdit.setPlaceholderText(
            "例如：1073768508 或 mirror.chromaso.net/thread/1073768508"
        )
        self.urlEdit.setClearButtonEnabled(True)
        self.urlEdit.setMinimumWidth(440)

        self.fetchButton = PrimaryPushButton("读取主题", query_card)
        self.fetchButton.setIcon(FIF.SEARCH)
        query_row.addWidget(self.urlEdit, 1)
        query_row.addWidget(self.fetchButton)
        query_layout.addLayout(query_row)
        root.addWidget(query_card)

        # 主题标题和发帖人统一放在一个大内容区域中，强化信息层级。
        content_card = CardWidget(self)
        content_layout = QVBoxLayout(content_card)
        content_layout.setContentsMargins(20, 18, 20, 18)
        content_layout.setSpacing(12)

        title_panel = QFrame(content_card)
        self.threadTitlePanel = title_panel
        title_panel.setObjectName("threadTitlePanel")
        title_panel.setStyleSheet(
            "QFrame#threadTitlePanel {"
            "background: rgba(0, 120, 212, 0.10);"
            "border: 1px solid rgba(0, 120, 212, 0.24);"
            "border-radius: 10px;"
            "}"
        )
        title_layout = QVBoxLayout(title_panel)
        title_layout.setContentsMargins(18, 14, 18, 14)
        title_layout.setSpacing(5)
        title_top_row = QHBoxLayout()
        title_top_row.addWidget(CaptionLabel("当前主题", title_panel))
        title_top_row.addStretch(1)
        self.viewThreadButton = PushButton("查看帖子", title_panel)
        self.viewThreadButton.setIcon(FIF.VIEW)
        self.viewThreadButton.setEnabled(False)
        title_top_row.addWidget(self.viewThreadButton)
        title_layout.addLayout(title_top_row)
        self.threadTitleLabel = TitleLabel("尚未读取主题", title_panel)
        self.threadSummaryLabel = CaptionLabel("", title_panel)
        self.threadTitleLabel.setWordWrap(True)
        title_layout.addWidget(self.threadTitleLabel)
        title_layout.addWidget(self.threadSummaryLabel)
        content_layout.addWidget(title_panel)

        author_panel = CardWidget(content_card)
        author_layout = QVBoxLayout(author_panel)
        author_layout.setContentsMargins(14, 14, 14, 14)
        author_layout.setSpacing(10)
        table_heading = QHBoxLayout()
        table_heading.addWidget(SubtitleLabel("发帖人", author_panel))
        table_heading.addStretch(1)
        self.selectionLabel = CaptionLabel("已选择 0 人", author_panel)
        table_heading.addWidget(self.selectionLabel)
        author_layout.addLayout(table_heading)

        self.authorTable = TableWidget(author_panel)
        select_header = SelectAllHeader(self.authorTable)
        self.authorTable.setHorizontalHeader(select_header)
        self.selectAllRadio = select_header.selectButton
        self.authorTable.setColumnCount(5)
        self.authorTable.setHorizontalHeaderLabels(
            ["", "头像", "发帖人名称", "发言数", "主页链接"]
        )
        self.authorTable.verticalHeader().hide()
        self.authorTable.setBorderVisible(True)
        self.authorTable.setBorderRadius(8)
        self.authorTable.setWordWrap(False)
        self.authorTable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.authorTable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.authorTable.setSelectionMode(QAbstractItemView.SingleSelection)
        self.authorTable.setIconSize(QSize(42, 42))
        header = self.authorTable.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.authorTable.setColumnWidth(0, 64)
        self.authorTable.setColumnWidth(1, 70)
        self.authorTable.setColumnWidth(3, 82)
        self.authorTable.setMinimumHeight(270)
        author_layout.addWidget(self.authorTable, 1)
        content_layout.addWidget(author_panel, 1)
        root.addWidget(content_card, 1)

        export_card = CardWidget(self)
        export_layout = QVBoxLayout(export_card)
        export_layout.setContentsMargins(18, 14, 18, 14)
        export_layout.setSpacing(9)
        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        self.outputEdit = LineEdit(export_card)
        self.outputEdit.setPlaceholderText("导出目录")
        self.outputEdit.setText(str(Path.cwd()))
        self.browseButton = PushButton("选择目录", export_card)
        self.browseButton.setIcon(FIF.FOLDER)
        self.formatCombo = ComboBox(export_card)
        self.formatCombo.addItems(["HTML", "PDF", "EPUB"])
        self.formatCombo.setFixedWidth(110)
        self.exportButton = PrimaryPushButton("导出", export_card)
        self.exportButton.setIcon(FIF.SAVE)
        self.exportButton.setEnabled(False)
        export_row.addWidget(self.outputEdit, 1)
        export_row.addWidget(self.browseButton)
        export_row.addWidget(self.formatCombo)
        export_row.addWidget(self.exportButton)
        export_layout.addLayout(export_row)

        status_row = QHBoxLayout()
        self.progressBar = IndeterminateProgressBar(export_card)
        self.progressBar.setFixedWidth(180)
        self.progressBar.setVisible(False)
        self.statusLabel = CaptionLabel("就绪", export_card)
        status_row.addWidget(self.progressBar)
        status_row.addWidget(self.statusLabel)
        status_row.addStretch(1)
        export_layout.addLayout(status_row)
        root.addWidget(export_card)

        self.fetchButton.clicked.connect(self.fetchRequested)
        self.viewThreadButton.clicked.connect(self.viewThreadRequested)
        self.urlEdit.returnPressed.connect(self.fetchRequested)
        self.browseButton.clicked.connect(self.browseRequested)
        self.exportButton.clicked.connect(self.exportRequested)
        select_header.toggled.connect(self.selectAllChanged)
        self.authorTable.cellClicked.connect(
            lambda row, column: self.profileRequested.emit(row)
            if column in {2, 4}
            else None
        )
        self.authorTable.itemSelectionChanged.connect(self._selection_changed)

    def set_busy(self, busy: bool) -> None:
        self.fetchButton.setEnabled(not busy)
        self.urlEdit.setEnabled(not busy)
        self.progressBar.setVisible(busy)
        if busy:
            self.progressBar.resume()
        else:
            self.progressBar.pause()

    def set_has_data(self, has_data: bool) -> None:
        self.hasData = has_data
        self.viewThreadButton.setEnabled(has_data)
        self.exportButton.setEnabled(has_data and self.selected_count() > 0)

    def set_accent_color(self, color: QColor) -> None:
        self.title_panel_color = QColor(color)
        self.threadTitlePanel.setStyleSheet(
            "QFrame#threadTitlePanel {"
            f"background: rgba({color.red()}, {color.green()}, {color.blue()}, 26);"
            f"border: 1px solid rgba({color.red()}, {color.green()}, {color.blue()}, 70);"
            "border-radius: 10px;"
            "}"
        )

    def _selection_changed(self) -> None:
        row = self.authorTable.currentRow()
        if row >= 0:
            radio = self.authorTable.cellWidget(row, 0)
            if radio:
                button = radio.findChild(RadioButton)
                if button:
                    button.setChecked(True)

    def selected_count(self) -> int:
        count = 0
        for row in range(self.authorTable.rowCount()):
            cell = self.authorTable.cellWidget(row, 0)
            button = cell.findChild(RadioButton) if cell else None
            if button and button.isChecked():
                count += 1
        return count

    def author_selection_updated(self) -> None:
        count = self.selected_count()
        total = self.authorTable.rowCount()
        self.selectionLabel.setText(f"已选择 {count} 人")
        self.selectAllRadio.blockSignals(True)
        self.selectAllRadio.setChecked(total > 0 and count == total)
        self.selectAllRadio.blockSignals(False)
        self.exportButton.setEnabled(self.hasData and count > 0)
        self.authorSelectionChanged.emit()


class ContentInterface(QWidget):
    linkActivated = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contentInterface")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        header = QHBoxLayout()
        self.titleLabel = SubtitleLabel("内容浏览", self)
        header.addWidget(self.titleLabel)
        header.addStretch(1)
        self.progressBar = IndeterminateProgressBar(self)
        self.progressBar.setFixedWidth(180)
        self.progressBar.setVisible(False)
        header.addWidget(self.progressBar)
        root.addLayout(header)

        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)
        self.browser = QTextBrowser(card)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setFrameShape(QFrame.NoFrame)
        self.browser.anchorClicked.connect(
            lambda url: self.linkActivated.emit(url.toString())
        )
        card_layout.addWidget(self.browser)
        root.addWidget(card, 1)

    def set_loading(self, title: str) -> None:
        self.titleLabel.setText(title)
        self.browser.clear()
        self.progressBar.setVisible(True)
        self.progressBar.resume()

    def set_html(self, title: str, content: str) -> None:
        self.titleLabel.setText(title)
        self.progressBar.pause()
        self.progressBar.setVisible(False)
        self.browser.setHtml(content)
        self.browser.moveCursor(QTextCursor.Start)

    def set_error(self, message: str) -> None:
        self.progressBar.pause()
        self.progressBar.setVisible(False)
        self.browser.setHtml(
            f"<h2>加载失败</h2><p>{html.escape(message)}</p>"
        )


class LogInterface(QWidget):
    """仅保存在内存中的三级实时日志页面。"""

    clearRequested = pyqtSignal()
    filterChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logInterface")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(14)
        root.addWidget(SubtitleLabel("实时日志", self))

        toolbar_card = CardWidget(self)
        toolbar = QHBoxLayout(toolbar_card)
        toolbar.setContentsMargins(16, 12, 16, 12)
        toolbar.addWidget(BodyLabel("日志级别", toolbar_card))
        self.levelCombo = ComboBox(toolbar_card)
        self.levelCombo.addItems(["全部", "INFO", "WARNING", "ERROR"])
        self.levelCombo.setFixedWidth(130)
        toolbar.addWidget(self.levelCombo)
        toolbar.addStretch(1)
        self.countLabel = CaptionLabel("0 条日志", toolbar_card)
        toolbar.addWidget(self.countLabel)
        self.clearButton = PushButton("清空", toolbar_card)
        self.clearButton.setIcon(FIF.DELETE)
        toolbar.addWidget(self.clearButton)
        root.addWidget(toolbar_card)

        log_card = CardWidget(self)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 12, 12, 12)
        self.logEdit = TextEdit(log_card)
        self.logEdit.setReadOnly(True)
        self.logEdit.setPlaceholderText("运行日志将在这里实时显示……")
        self.logEdit.setStyleSheet(
            "TextEdit { font-family: 'Cascadia Mono', 'Consolas'; font-size: 13px; }"
        )
        log_layout.addWidget(self.logEdit)
        root.addWidget(log_card, 1)

        self.clearButton.clicked.connect(self.clearRequested)
        self.levelCombo.currentTextChanged.connect(self.filterChanged)


class EmptyInterface(QWidget):
    """预留导航页面，当前不放置任何内容。"""

    def __init__(self, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)


class SettingsInterface(QWidget):
    saveRequested = pyqtSignal()
    restoreDefaultsRequested = pyqtSignal()
    dirtyChanged = pyqtSignal(bool)
    accentPreviewChanged = pyqtSignal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsInterface")
        self._dirty = False
        self._loading = False
        self.directoryEdits: dict[str, LineEdit] = {}
        self._build_ui()

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("设置", self))
        header.addStretch(1)
        self.restoreButton = PushButton("恢复默认设置", self)
        self.restoreButton.setIcon(FIF.RETURN)
        self.restoreButton.clicked.connect(self.restoreDefaultsRequested)
        header.addWidget(self.restoreButton)
        self.saveButton = PrimaryPushButton("保存", self)
        self.saveButton.setIcon(FIF.SAVE)
        self.saveButton.clicked.connect(self.saveRequested)
        header.addWidget(self.saveButton)
        root.addLayout(header)

        directory_card = CardWidget(self)
        directory_layout = QVBoxLayout(directory_card)
        directory_layout.setContentsMargins(18, 16, 18, 18)
        directory_layout.setSpacing(12)
        directory_layout.addWidget(StrongBodyLabel("默认导出目录", directory_card))
        for fmt in ("HTML", "PDF", "EPUB"):
            row = QHBoxLayout()
            label = BodyLabel(fmt, directory_card)
            label.setFixedWidth(54)
            edit = LineEdit(directory_card)
            button = PushButton("选择目录", directory_card)
            button.setIcon(FIF.FOLDER)
            button.clicked.connect(lambda _checked=False, f=fmt: self._browse_directory(f))
            row.addWidget(label)
            row.addWidget(edit, 1)
            row.addWidget(button)
            directory_layout.addLayout(row)
            self.directoryEdits[fmt] = edit
            edit.textChanged.connect(self._mark_dirty)
        root.addWidget(directory_card)

        general_card = CardWidget(self)
        general_layout = QVBoxLayout(general_card)
        general_layout.setContentsMargins(18, 16, 18, 18)
        general_layout.setSpacing(14)
        general_layout.addWidget(StrongBodyLabel("导出与下载", general_card))

        format_row = QHBoxLayout()
        format_row.addWidget(BodyLabel("默认导出格式", general_card))
        format_row.addStretch(1)
        self.defaultFormatCombo = ComboBox(general_card)
        self.defaultFormatCombo.addItems(["HTML", "PDF", "EPUB"])
        self.defaultFormatCombo.setFixedWidth(140)
        format_row.addWidget(self.defaultFormatCombo)
        general_layout.addLayout(format_row)

        delay_row = QHBoxLayout()
        delay_row.addWidget(BodyLabel("帖子翻页间隔", general_card))
        delay_row.addStretch(1)
        self.pageDelaySpin = DoubleSpinBox(general_card)
        self.pageDelaySpin.setRange(0, 60)
        self.pageDelaySpin.setDecimals(1)
        self.pageDelaySpin.setSingleStep(0.1)
        self.pageDelaySpin.setFixedWidth(110)
        delay_row.addWidget(self.pageDelaySpin)
        delay_row.addWidget(BodyLabel("秒", general_card))
        general_layout.addLayout(delay_row)

        color_row = QHBoxLayout()
        color_row.addWidget(BodyLabel("界面强调色", general_card))
        color_row.addStretch(1)
        self.accentColorButton = ColorPickerButton(
            QColor("#0078D4"), "界面强调色", general_card
        )
        color_row.addWidget(self.accentColorButton)
        general_layout.addLayout(color_row)
        root.addWidget(general_card)
        root.addStretch(1)

        self.defaultFormatCombo.currentTextChanged.connect(self._mark_dirty)
        self.pageDelaySpin.valueChanged.connect(self._mark_dirty)
        self.accentColorButton.colorChanged.connect(self._on_accent_changed)

    def _on_accent_changed(self, color: QColor) -> None:
        self._mark_dirty()
        self.accentPreviewChanged.emit(color)

    def _browse_directory(self, fmt: str) -> None:
        current = self.directoryEdits[fmt].text().strip() or str(Path.cwd())
        selected = QFileDialog.getExistingDirectory(self, f"选择 {fmt} 默认目录", current)
        if selected:
            self.directoryEdits[fmt].setText(selected)

    def _mark_dirty(self, *_args) -> None:
        if self._loading or self._dirty:
            return
        self._dirty = True
        self.saveButton.setText("保存*")
        self.dirtyChanged.emit(True)

    def mark_saved(self) -> None:
        self._dirty = False
        self.saveButton.setText("保存")
        self.dirtyChanged.emit(False)

    def load_values(self, values: dict[str, object]) -> None:
        self._loading = True
        for fmt in ("HTML", "PDF", "EPUB"):
            self.directoryEdits[fmt].setText(str(values[f"directory/{fmt.lower()}"]))
        self.defaultFormatCombo.setCurrentText(str(values["default_format"]))
        self.pageDelaySpin.setValue(float(values["page_delay"]))
        self.accentColorButton.setColor(QColor(str(values["accent_color"])))
        self._loading = False
        self.mark_saved()

    def values(self) -> dict[str, object]:
        result: dict[str, object] = {
            "default_format": self.defaultFormatCombo.currentText(),
            "page_delay": self.pageDelaySpin.value(),
            "accent_color": self.accentColorButton.color.name(),
        }
        for fmt, edit in self.directoryEdits.items():
            result[f"directory/{fmt.lower()}"] = edit.text().strip()
        return result


class MainWindow(FluentWindow):
    MAX_LOGS = 5000

    def __init__(self):
        super().__init__()
        self.downloadPage = DownloadInterface(self)
        self.searchPage = EmptyInterface("searchInterface", self)
        self.contentPage = ContentInterface(self)
        self.mirrorHomePage = EmptyInterface("mirrorHomeInterface", self)
        self.logPage = LogInterface(self)
        self.settingsPage = SettingsInterface(self)
        self.configPath = Path(__file__).resolve().parent / "settings.json"
        self._savedSettingsValues: dict[str, object] = {}
        self._navigationGuard = False
        self._previousInterface: QWidget = self.downloadPage
        self.threadData: ThreadData | None = None
        self.authors: list[Author] = []
        self.authorRows: dict[str, int] = {}
        self.logEntries: list[tuple[str, str, str]] = []
        self.fetchWorker: FetchWorker | None = None
        self.avatarWorker: AvatarWorker | None = None
        self.profileWorker: ProfileWorker | None = None
        self.viewerThreadWorker: ViewerThreadWorker | None = None
        self.profileGeneration = 0
        self.viewerGeneration = 0
        self.avatarBytes: dict[str, bytes] = {}
        self.contentAuthors: dict[str, Author] = {}
        self.activeWorkers: list[QThread] = []
        self.accentColor = QColor("#0078D4")
        self.generation = 0

        self._init_navigation()
        self._connect_signals()
        self._load_settings()
        self._init_window()

    def _init_navigation(self) -> None:
        self.addSubInterface(self.downloadPage, FIF.DOWNLOAD, "主题下载")
        self.addSubInterface(self.searchPage, FIF.SEARCH, "搜索")
        self.addSubInterface(self.contentPage, FIF.DOCUMENT, "内容浏览")
        self.addSubInterface(self.mirrorHomePage, FIF.GLOBE, "镜像主页")
        self.addSubInterface(self.logPage, FIF.HISTORY, "实时日志")
        self.addSubInterface(
            self.settingsPage,
            FIF.SETTING,
            "设置",
            NavigationItemPosition.BOTTOM,
        )
        # 禁用页面切换动画，规避 qfluentwidgets 1.11.x 在动画结束时
        # 重复 disconnect 信号导致的崩溃。
        self.stackedWidget.setAnimationEnabled(False)

    def _connect_signals(self) -> None:
        page = self.downloadPage
        page.fetchRequested.connect(self.start_fetch)
        page.viewThreadRequested.connect(self.show_current_thread)
        page.browseRequested.connect(self.choose_output)
        page.exportRequested.connect(self.export_selected)
        page.selectAllChanged.connect(self.set_all_authors_selected)
        page.profileRequested.connect(self.open_profile)
        self.contentPage.linkActivated.connect(self.on_content_link)
        self.logPage.clearRequested.connect(self.clear_logs)
        self.logPage.filterChanged.connect(self.render_logs)
        self.settingsPage.saveRequested.connect(self.save_settings)
        self.settingsPage.restoreDefaultsRequested.connect(self.restore_default_settings)
        self.settingsPage.accentPreviewChanged.connect(self.apply_accent_color)
        self.downloadPage.formatCombo.currentTextChanged.connect(
            self.apply_format_default_directory
        )
        self.stackedWidget.currentChanged.connect(self.on_interface_changed)

    def _init_window(self) -> None:
        self.resize(1180, 820)
        self.setMinimumSize(960, 700)
        self.setWindowTitle("M系镜像 · 单主题下载器")
        desktop = QApplication.desktop().availableGeometry()
        self.move(
            desktop.center().x() - self.width() // 2,
            desktop.center().y() - self.height() // 2,
        )

    def track_worker(self, worker: QThread) -> None:
        """保留所有后台线程，避免快速切换内容时线程被提前销毁。"""
        self.activeWorkers.append(worker)

        def remove_worker() -> None:
            if worker in self.activeWorkers:
                self.activeWorkers.remove(worker)

        worker.finished.connect(remove_worker)

    def default_settings_values(self) -> dict[str, object]:
        default_directory = str(Path(__file__).resolve().parent)
        return {
            "directory/html": default_directory,
            "directory/pdf": default_directory,
            "directory/epub": default_directory,
            "default_format": "HTML",
            "page_delay": 0.8,
            "accent_color": "#0078D4",
        }

    def _load_settings(self) -> None:
        values = self.default_settings_values()
        if self.configPath.exists():
            try:
                loaded = json.loads(self.configPath.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key in values:
                        if key in loaded:
                            values[key] = loaded[key]
            except (OSError, ValueError, TypeError) as exc:
                self.append_log(
                    "WARNING", time.strftime("%H:%M:%S"), f"读取设置失败：{exc}"
                )
        self._savedSettingsValues = dict(values)
        self.settingsPage.load_values(values)
        default_format = str(values["default_format"])
        self.downloadPage.formatCombo.setCurrentText(default_format)
        self.apply_format_default_directory(default_format)
        self.apply_accent_color(QColor(str(values["accent_color"])))

    def save_settings(self, show_notice: bool = True) -> bool:
        values = self.settingsPage.values()
        for fmt in ("HTML", "PDF", "EPUB"):
            value = str(values[f"directory/{fmt.lower()}"]).strip()
            if not value:
                self.show_info("设置错误", f"请选择 {fmt} 默认导出目录。", "ERROR")
                return False
            path = Path(value).expanduser()
            if path.exists() and not path.is_dir():
                self.show_info("设置错误", f"{fmt} 导出路径不是目录。", "ERROR")
                return False
        try:
            temporary = self.configPath.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.configPath)
        except OSError as exc:
            self.show_info("保存失败", str(exc), "ERROR")
            return False
        self._savedSettingsValues = dict(values)
        self.settingsPage.mark_saved()
        default_format = str(values["default_format"])
        self.downloadPage.formatCombo.setCurrentText(default_format)
        self.apply_format_default_directory(default_format)
        self.apply_accent_color(QColor(str(values["accent_color"])))
        if show_notice:
            self.show_info("已保存", "设置已保存。")
        return True

    def restore_default_settings(self) -> None:
        values = self.default_settings_values()
        self.settingsPage.load_values(values)
        self.settingsPage._mark_dirty()
        self.apply_accent_color(QColor(str(values["accent_color"])))

    def apply_accent_color(self, color: QColor) -> None:
        if not color.isValid():
            color = QColor("#0078D4")
        self.accentColor = QColor(color)
        setThemeColor(self.accentColor)
        self.downloadPage.set_accent_color(self.accentColor)

    def apply_format_default_directory(self, output_format: str) -> None:
        key = f"directory/{output_format.lower()}"
        value = self.settingsPage.values().get(key, "")
        if value:
            self.downloadPage.outputEdit.setText(str(value))

    def on_interface_changed(self, index: int) -> None:
        if self._navigationGuard:
            return
        target = self.stackedWidget.widget(index)
        if (
            self._previousInterface is self.settingsPage
            and target is not self.settingsPage
            and self.settingsPage.is_dirty
        ):
            dialog = MessageBox(
                "设置尚未保存",
                "请选择如何处理尚未保存的修改。",
                self,
            )
            dialog.yesButton.setText("保存并切换")
            dialog.cancelButton.setText("取消")
            discard_button = PushButton("不保存并切换", dialog.buttonGroup)
            dialog.buttonLayout.insertWidget(1, discard_button, 1, Qt.AlignVCenter)
            dialog.buttonGroup.setMinimumWidth(540)
            dialog.widget.setFixedWidth(max(600, dialog.widget.width()))
            choice = {"discard": False}

            def discard_changes() -> None:
                choice["discard"] = True
                dialog.reject()

            discard_button.clicked.connect(discard_changes)
            if dialog.exec():
                if self.save_settings(show_notice=False):
                    self._previousInterface = target
                    return
            elif choice["discard"]:
                self._load_settings()
                self._previousInterface = target
                return

            # 取消，或保存校验失败：下一轮事件循环再返回设置页，避免在
            # stackedWidget.currentChanged 信号内部重入页面切换。
            self._previousInterface = self.settingsPage

            def return_to_settings() -> None:
                self._navigationGuard = True
                self.switchTo(self.settingsPage)
                self._navigationGuard = False

            QTimer.singleShot(0, return_to_settings)
            return
        self._previousInterface = target

    def show_info(self, title: str, content: str, level: str = "INFO") -> None:
        kwargs = dict(
            title=title,
            content=content,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3500,
            parent=self,
        )
        if level == "ERROR":
            InfoBar.error(**kwargs)
        elif level == "WARNING":
            InfoBar.warning(**kwargs)
        elif title == "完成":
            InfoBar.success(**kwargs)
        else:
            InfoBar.info(**kwargs)

    def show_export_success(self, output: Path, count: int) -> None:
        bar = InfoBar.success(
            title="导出完成",
            content=f"已导出 {count} 条发言。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=10000,
            parent=self,
        )
        open_directory = PushButton("打开目录", bar)
        open_file = PrimaryPushButton("打开文件", bar)
        open_directory.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.parent)))
        )
        open_file.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
        )
        bar.addWidget(open_directory)
        bar.addWidget(open_file)

    def start_fetch(self) -> None:
        try:
            url = normalize_url(self.downloadPage.urlEdit.text())
        except ValueError as exc:
            self.show_info("输入错误", str(exc), "ERROR")
            return

        if self.avatarWorker and self.avatarWorker.isRunning():
            self.avatarWorker.requestInterruption()
        self.generation += 1
        generation = self.generation
        self.threadData = None
        self.authors = []
        self.authorRows.clear()
        self.avatarBytes.clear()
        self.downloadPage.authorTable.setRowCount(0)
        self.downloadPage.selectAllRadio.blockSignals(True)
        self.downloadPage.selectAllRadio.setChecked(False)
        self.downloadPage.selectAllRadio.blockSignals(False)
        self.downloadPage.selectionLabel.setText("已选择 0 人")
        self.downloadPage.set_has_data(False)
        self.downloadPage.set_busy(True)
        self.downloadPage.threadTitleLabel.setText("正在读取主题……")
        self.downloadPage.threadSummaryLabel.setText("正在获取全部分页，请稍候。")
        self.downloadPage.statusLabel.setText("正在连接……")
        self.clear_logs()
        self.append_log("INFO", time.strftime("%H:%M:%S"), "用户开始读取主题")

        worker = FetchWorker(
            generation,
            url,
            self.settingsPage.pageDelaySpin.value(),
            20,
            self,
        )
        self.fetchWorker = worker
        worker.progressChanged.connect(self.on_progress)
        worker.dataReady.connect(self.on_data_ready)
        worker.failed.connect(self.on_fetch_failed)
        worker.logCreated.connect(self.append_log)
        self.track_worker(worker)
        worker.start()

    def on_progress(self, generation: int, page: int, posts: int) -> None:
        if generation == self.generation:
            self.downloadPage.statusLabel.setText(
                f"正在读取第 {page} 页，已获取 {posts} 条发言……"
            )

    def on_data_ready(
        self, generation: int, data: ThreadData, downloader: ThreadDownloader
    ) -> None:
        if generation != self.generation:
            return
        self.threadData = data
        self.authors = data.authors()
        self.populate_authors()
        self.downloadPage.set_busy(False)
        self.downloadPage.set_has_data(True)
        self.downloadPage.threadTitleLabel.setText(data.title)
        self.downloadPage.threadSummaryLabel.setText(
            f"共 {data.page_count} 页 · {len(data.posts)} 条发言 · {len(self.authors)} 位发帖人"
        )
        self.downloadPage.statusLabel.setText("主题读取完成，正在加载头像……")
        self.show_info("完成", "主题全部分页已读取完成。")

        avatar_worker = AvatarWorker(generation, downloader, self.authors, self)
        self.avatarWorker = avatar_worker
        avatar_worker.avatarReady.connect(self.on_avatar_ready)
        avatar_worker.allDone.connect(self.on_avatars_done)
        avatar_worker.logCreated.connect(self.append_log)
        self.track_worker(avatar_worker)
        avatar_worker.start()

    def on_fetch_failed(self, generation: int, message: str) -> None:
        if generation != self.generation:
            return
        self.downloadPage.set_busy(False)
        self.downloadPage.set_has_data(False)
        self.downloadPage.threadTitleLabel.setText("读取失败")
        self.downloadPage.threadSummaryLabel.setText(message)
        self.downloadPage.statusLabel.setText("发生错误，可前往“实时日志”查看详情。")
        self.append_log("ERROR", time.strftime("%H:%M:%S"), message)
        self.show_info("读取失败", message, "ERROR")

    def populate_authors(self) -> None:
        table = self.downloadPage.authorTable
        table.setRowCount(len(self.authors))
        self.authorRows.clear()
        for row, author in enumerate(self.authors):
            self.authorRows[author.key] = row
            table.setRowHeight(row, 60)
            selector_cell = QWidget(table)
            selector_layout = QHBoxLayout(selector_cell)
            selector_layout.setContentsMargins(0, 0, 0, 0)
            selector_layout.setAlignment(Qt.AlignCenter)
            selector = RadioButton("", selector_cell)
            selector.setAutoExclusive(False)
            selector.toggled.connect(self.downloadPage.author_selection_updated)
            selector_layout.addWidget(selector)
            avatar_item = QTableWidgetItem()
            avatar_item.setIcon(QIcon(self.make_placeholder(author.name)))
            name_item = QTableWidgetItem(author.name)
            name_item.setToolTip("单击查看作者个人主页与公开主题")
            name_font = name_item.font()
            name_font.setUnderline(True)
            name_item.setFont(name_font)
            count_item = QTableWidgetItem(str(author.post_count))
            count_item.setTextAlignment(Qt.AlignCenter)
            profile_item = QTableWidgetItem(author.profile_url or "（无公开主页）")
            profile_item.setToolTip(author.profile_url)
            if author.profile_url:
                profile_font = profile_item.font()
                profile_font.setUnderline(True)
                profile_item.setFont(profile_font)
            table.setCellWidget(row, 0, selector_cell)
            table.setItem(row, 1, avatar_item)
            table.setItem(row, 2, name_item)
            table.setItem(row, 3, count_item)
            table.setItem(row, 4, profile_item)
        table.clearSelection()
        table.setCurrentCell(-1, -1)
        self.downloadPage.author_selection_updated()

    def make_placeholder(self, name: str) -> QPixmap:
        pixmap = QPixmap(42, 42)
        background = QColor(self.accentColor)
        background.setAlpha(45)
        pixmap.fill(background)
        painter = QPainter(pixmap)
        painter.setPen(self.accentColor)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, (name[:1] or "?").upper())
        painter.end()
        return pixmap

    def on_avatar_ready(self, generation: int, author_key: str, raw: bytes) -> None:
        if generation != self.generation or author_key not in self.authorRows:
            return
        self.avatarBytes[author_key] = raw
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            return
        pixmap = pixmap.scaled(
            42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        item = self.downloadPage.authorTable.item(self.authorRows[author_key], 1)
        if item:
            item.setIcon(QIcon(pixmap))

    def on_avatars_done(self, generation: int) -> None:
        if generation == self.generation:
            self.downloadPage.statusLabel.setText(
                "就绪：请选择作者，或直接导出全部内容。"
            )

    def choose_output(self) -> None:
        current = self.downloadPage.outputEdit.text().strip() or str(Path.cwd())
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择导出目录",
            current,
        )
        if selected:
            self.downloadPage.outputEdit.setText(selected)

    def output_directory(self) -> Path | None:
        value = self.downloadPage.outputEdit.text().strip()
        if not value:
            self.show_info("输出错误", "请选择导出目录。", "ERROR")
            return None
        path = Path(value).expanduser()
        if path.exists() and not path.is_dir():
            self.show_info("输出错误", "当前路径不是目录。", "ERROR")
            return None
        return path

    def selected_author_keys(self) -> set[str]:
        keys: set[str] = set()
        table = self.downloadPage.authorTable
        for row, author in enumerate(self.authors):
            cell = table.cellWidget(row, 0)
            button = cell.findChild(RadioButton) if cell else None
            if button and button.isChecked():
                keys.add(author.key)
        return keys

    def set_all_authors_selected(self, checked: bool) -> None:
        table = self.downloadPage.authorTable
        for row in range(table.rowCount()):
            cell = table.cellWidget(row, 0)
            button = cell.findChild(RadioButton) if cell else None
            if button:
                button.blockSignals(True)
                button.setChecked(checked)
                button.blockSignals(False)
        self.downloadPage.author_selection_updated()

    def export_selected(self) -> None:
        if not self.threadData:
            return
        keys = self.selected_author_keys()
        if not keys:
            self.show_info("尚未选择", "至少选择一位发帖人。", "WARNING")
            return
        directory = self.output_directory()
        if directory:
            self.export(
                self.threadData,
                directory,
                self.downloadPage.formatCombo.currentText(),
                keys,
            )

    def export(
        self,
        data: ThreadData,
        directory: Path,
        output_format: str,
        author_keys: set[str],
    ) -> None:
        try:
            output = export_thread(data, directory, output_format, author_keys)
            count = len(data.posts_by_authors(author_keys))
            message = f"已导出 {count} 条发言：{output.resolve()}"
            self.downloadPage.statusLabel.setText(message)
            self.append_log("INFO", time.strftime("%H:%M:%S"), message)
            self.show_export_success(output.resolve(), count)
        except (OSError, RuntimeError, ValueError) as exc:
            self.append_log("ERROR", time.strftime("%H:%M:%S"), f"保存失败：{exc}")
            self.show_info("保存失败", str(exc), "ERROR")

    def circular_avatar_uri(
        self, author_key: str, name: str, avatar_bytes: dict[str, bytes], size: int = 56
    ) -> str:
        source = QPixmap()
        raw = avatar_bytes.get(author_key, b"")
        if not raw or not source.loadFromData(raw):
            source = self.make_placeholder(name)
        source = source.scaled(
            size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        target = QPixmap(size, size)
        target.fill(Qt.transparent)
        painter = QPainter(target)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addEllipse(0, 0, size, size)
        painter.setClipPath(path)
        x = (source.width() - size) // 2
        y = (source.height() - size) // 2
        painter.drawPixmap(0, 0, source, x, y, size, size)
        painter.end()
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.WriteOnly)
        target.save(buffer, "PNG")
        return "data:image/png;base64," + base64.b64encode(bytes(encoded)).decode("ascii")

    def build_thread_view_html(
        self, data: ThreadData, avatar_bytes: dict[str, bytes]
    ) -> str:
        articles: list[str] = []
        for post in data.posts:
            key = post.profile_url or post.author
            avatar = self.circular_avatar_uri(key, post.author, avatar_bytes)
            author = html.escape(post.author)
            if post.profile_url:
                author = (
                    f'<a href="{html.escape(post.profile_url, quote=True)}">{author}</a>'
                )
            body = render_post_body_html(data, post, max_image_width=820)
            articles.append(
                '<section class="post">'
                '<div class="post-head">'
                f'<img class="avatar" src="{avatar}" width="52" height="52"/>'
                '<div class="post-meta">'
                f'<div class="author">{author}</div>'
                f'<div class="time">{html.escape(post.published)} · 第 {post.floor} 楼</div>'
                '</div></div>'
                f'<div class="body">{body}</div>'
                '</section>'
            )
        return f'''<!doctype html><html><head><meta charset="utf-8"/>
<style>
body {{ margin: 8px; color: #202124; font-family: "Microsoft YaHei", Arial, sans-serif; }}
h1 {{ color: {self.accentColor.name()}; margin: 4px 0 18px; }}
.post {{ border: 1px solid #d9e2ec; margin: 0 0 14px; padding: 14px; }}
.post-head {{ margin-bottom: 12px; }}
.avatar {{ float: left; margin-right: 12px; }}
.post-meta {{ min-height: 54px; padding-top: 4px; }}
.author {{ font-size: 16px; font-weight: bold; }}
.author a {{ color: {self.accentColor.name()}; text-decoration: none; }}
.time {{ color: #667085; font-size: 12px; margin-top: 5px; }}
.body {{ clear: both; line-height: 1.65; word-break: normal; }}
.body img {{ height: auto; max-width: 100%; }}
.body a {{ color: {self.accentColor.name()}; text-decoration: underline; }}
.body blockquote {{ border-left: 3px solid {self.accentColor.name()}; margin-left: 0; padding-left: 10px; }}
</style></head><body><h1>{html.escape(data.title)}</h1>{''.join(articles)}</body></html>'''

    def show_current_thread(self) -> None:
        if not self.threadData:
            return
        self.contentAuthors = {
            QUrl(author.profile_url).path(): author
            for author in self.threadData.authors()
            if author.profile_url
        }
        self.contentPage.set_html(
            self.threadData.title,
            self.build_thread_view_html(self.threadData, self.avatarBytes),
        )
        self.switchTo(self.contentPage)

    def open_profile(self, row: int) -> None:
        if not (0 <= row < len(self.authors)):
            return
        author = self.authors[row]
        if not author.profile_url:
            self.show_info("没有主页", "该发帖人没有公开主页链接。", "WARNING")
            return
        self.load_author_profile(author)

    def load_author_profile(self, author: Author) -> None:
        self.profileGeneration += 1
        generation = self.profileGeneration
        if self.profileWorker and self.profileWorker.isRunning():
            self.profileWorker.requestInterruption()
        self.contentPage.set_loading(f"{author.name} · 个人主页")
        self.switchTo(self.contentPage)
        worker = ProfileWorker(
            generation, author, self.settingsPage.pageDelaySpin.value(), self
        )
        self.profileWorker = worker
        worker.profileReady.connect(self.on_profile_ready)
        worker.failed.connect(self.on_profile_failed)
        worker.progressChanged.connect(self.on_profile_progress)
        worker.logCreated.connect(self.append_log)
        self.track_worker(worker)
        worker.start()

    def on_profile_progress(self, generation: int, page: int, works: int) -> None:
        if generation == self.profileGeneration:
            self.contentPage.titleLabel.setText(
                f"正在读取作者作品 · 第 {page} 页 · {works} 个主题"
            )

    def on_profile_ready(self, generation: int, profile: AuthorProfileData) -> None:
        if generation != self.profileGeneration:
            return
        key = profile.profile_url or profile.name
        avatar = self.circular_avatar_uri(key, profile.name, self.avatarBytes, 72)
        works = []
        for work in profile.works:
            details = " · ".join(
                part
                for part in (
                    work.forum,
                    f"{work.reply_count} 回复" if work.reply_count else "",
                    f"发布于 {work.published}" if work.published else "",
                    f"更新于 {work.last_updated}" if work.last_updated else "",
                )
                if part
            )
            works.append(
                '<div class="work">'
                f'<a class="work-title" href="{html.escape(work.url, quote=True)}">'
                f'{html.escape(work.title)}</a>'
                f'<div class="work-meta">{html.escape(details)}</div>'
                '</div>'
            )
        content = f'''<!doctype html><html><head><meta charset="utf-8"/>
<style>
body {{ margin: 12px; color: #202124; font-family: "Microsoft YaHei", Arial, sans-serif; }}
.profile {{ border-bottom: 1px solid #d9e2ec; padding: 8px 4px 18px; margin-bottom: 14px; }}
.avatar {{ float: left; margin-right: 16px; }}
.name {{ color: {self.accentColor.name()}; font-size: 24px; font-weight: bold; padding-top: 8px; }}
.url {{ color: #667085; margin-top: 6px; }}
.works {{ clear: both; padding-top: 12px; }}
.work {{ border: 1px solid #d9e2ec; padding: 12px; margin-bottom: 10px; }}
.work-title {{ color: {self.accentColor.name()}; font-size: 16px; font-weight: bold; text-decoration: none; }}
.work-meta {{ color: #667085; margin-top: 6px; font-size: 12px; }}
</style></head><body>
<div class="profile"><img class="avatar" src="{avatar}" width="72" height="72"/>
<div class="name">{html.escape(profile.name)}</div>
<div class="url">{html.escape(profile.profile_url)}</div><div style="clear:both"></div></div>
<h2>公开主题作品（{len(profile.works)}）</h2>
<div class="works">{''.join(works) if works else '<p>没有搜索到公开主题。</p>'}</div>
</body></html>'''
        self.contentPage.set_html(f"{profile.name} · 个人主页", content)

    def on_profile_failed(self, generation: int, message: str) -> None:
        if generation == self.profileGeneration:
            self.contentPage.set_error(message)
            self.append_log("ERROR", time.strftime("%H:%M:%S"), message)

    def load_thread_in_viewer(self, thread_url: str) -> None:
        self.viewerGeneration += 1
        generation = self.viewerGeneration
        self.contentPage.set_loading("正在读取帖子")
        self.switchTo(self.contentPage)
        worker = ViewerThreadWorker(
            generation, thread_url, self.settingsPage.pageDelaySpin.value(), self
        )
        self.viewerThreadWorker = worker
        worker.dataReady.connect(self.on_viewer_thread_ready)
        worker.failed.connect(self.on_viewer_thread_failed)
        worker.logCreated.connect(self.append_log)
        self.track_worker(worker)
        worker.start()

    def on_viewer_thread_ready(
        self, generation: int, data: ThreadData, avatars: dict[str, bytes]
    ) -> None:
        if generation == self.viewerGeneration:
            self.avatarBytes.update(avatars)
            self.contentAuthors = {
                QUrl(author.profile_url).path(): author
                for author in data.authors()
                if author.profile_url
            }
            self.contentPage.set_html(
                data.title, self.build_thread_view_html(data, avatars)
            )

    def on_viewer_thread_failed(self, generation: int, message: str) -> None:
        if generation == self.viewerGeneration:
            self.contentPage.set_error(message)

    def on_content_link(self, value: str) -> None:
        url = QUrl(value)
        if url.host().lower() == "mirror.chromaso.net":
            path = url.path()
            if path.startswith("/thread/"):
                self.load_thread_in_viewer(value)
                return
            if path.startswith("/author/"):
                author = self.contentAuthors.get(path) or next(
                    (
                        item
                        for item in self.authors
                        if QUrl(item.profile_url).path() == path
                    ),
                    None,
                )
                if author:
                    self.load_author_profile(author)
                    return
        QDesktopServices.openUrl(url)

    def append_log(self, level: str, timestamp: str, message: str) -> None:
        level = level if level in {"INFO", "WARNING", "ERROR"} else "INFO"
        self.logEntries.append((level, timestamp, message))
        if len(self.logEntries) > self.MAX_LOGS:
            del self.logEntries[:500]
        self.render_logs()

    def clear_logs(self) -> None:
        self.logEntries.clear()
        self.render_logs()

    def render_logs(self, _filter: str | None = None) -> None:
        selected = self.logPage.levelCombo.currentText()
        colors = {"INFO": "#3a96dd", "WARNING": "#d98e04", "ERROR": "#d13438"}
        lines = []
        visible = 0
        for level, timestamp, message in self.logEntries:
            if selected not in {"全部", level}:
                continue
            visible += 1
            safe = html.escape(message).replace("\n", "<br>")
            lines.append(
                f'<div style="margin:3px 0"><span style="color:#7a7a7a">[{timestamp}]</span> '
                f'<b style="color:{colors[level]}">[{level}]</b> {safe}</div>'
            )
        self.logPage.logEdit.setHtml("".join(lines))
        self.logPage.logEdit.moveCursor(QTextCursor.End)
        self.logPage.countLabel.setText(
            f"显示 {visible} 条 / 共 {len(self.logEntries)} 条"
        )

    def closeEvent(self, event) -> None:
        running_workers = [
            worker
            for worker in self.activeWorkers
            if not isinstance(worker, AvatarWorker) and worker.isRunning()
        ]
        if running_workers:
            event.ignore()
            self.show_info(
                "任务仍在运行",
                "内容正在读取，请等待当前网络请求结束后再关闭窗口。",
                "WARNING",
            )
            return
        avatar_workers = [
            worker
            for worker in self.activeWorkers
            if isinstance(worker, AvatarWorker) and worker.isRunning()
        ]
        for worker in avatar_workers:
            worker.requestInterruption()
        if any(not worker.wait(2500) for worker in avatar_workers):
                event.ignore()
                self.show_info("正在结束任务", "头像任务即将结束，请稍后再关闭。", "WARNING")
                return
        super().closeEvent(event)


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    setTheme(Theme.AUTO)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
