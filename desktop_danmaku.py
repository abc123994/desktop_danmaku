from __future__ import annotations

"""Windows Firebase 即時桌面彈幕 Client。

功能：
1. 透過 Firebase Realtime Database SSE 長連線接收 Feed。
2. 啟動時忽略 Firebase 既有 snapshot，只顯示之後收到的新資料。
3. 以 Firebase created_at_ms 排序，並以 request_id 去重。
4. 保留系統匣、透明多螢幕 Overlay 與 Ctrl+Alt+D 輸入介面。
5. Ctrl+Alt+D 投稿透過 Firebase Anonymous Auth 寫入投稿佇列；不保存 管理員憑證。

Windows 啟動：
    直接執行 start_danmaku_firebase_py3146.bat

此程式只需要對外 HTTPS 連線到 Firebase，不需要 NATS、Socket 或入站 Port。
"""

import ctypes
import random
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


REQUIRED_PYTHON = (3, 14, 6)
APP_TITLE = "Firebase 即時桌面彈幕"
MAX_MESSAGE_LENGTH = 200


def _check_python_version() -> None:
    if sys.version_info[:2] != (3, 14) or sys.version_info[:3] < REQUIRED_PYTHON:
        required_text = ".".join(map(str, REQUIRED_PYTHON))
        current_text = ".".join(map(str, sys.version_info[:3]))
        message = (
            f"此程式需要 Python {required_text} 或更新的 3.14 維護版本。\n"
            f"目前版本：Python {current_text}"
        )

        if sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    message,
                    APP_TITLE,
                    0x00000010,
                )
            except AttributeError:
                pass

        raise SystemExit(message)


_check_python_version()

from PySide6.QtCore import (  # noqa: E402
    QAbstractNativeEventFilter,
    QObject,
    QPointF,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from firebase_feed import FirebaseFeedClient  # noqa: E402
from firebase_submit import (  # noqa: E402
    SubmissionError,
    new_request_id,
    submit_text,
)
from overlay_position import initial_message_x  # noqa: E402


# ============================================================
# Windows 單一執行個體
# ============================================================

ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_MUTEX_NAME = (
    # 與原 NATS Client 共用，避免新舊版本同時建立兩個 Overlay。
    r"Local\FirebaseDesktopDanmaku_MainRoom_9F5B7D21"
)

_single_instance_mutex_handle: int | None = None


def acquire_single_instance() -> bool:
    """防止同一台 Windows 電腦同時出現兩個彈幕 Overlay。"""

    global _single_instance_mutex_handle

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(
        None,
        False,
        SINGLE_INSTANCE_MUTEX_NAME,
    )

    if not handle:
        # Windows Mutex 建立失敗時不阻止程式啟動；錯誤仍由 OS/日誌呈現。
        return True

    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        ctypes.windll.user32.MessageBoxW(
            None,
            f"{APP_TITLE} 已經在執行。\n請查看 Windows 系統匣。",
            APP_TITLE,
            0x00000040,
        )
        return False

    _single_instance_mutex_handle = int(handle)
    return True


def release_single_instance() -> None:
    global _single_instance_mutex_handle

    if not _single_instance_mutex_handle:
        return

    try:
        ctypes.windll.kernel32.CloseHandle(
            wintypes.HANDLE(_single_instance_mutex_handle)
        )
    finally:
        _single_instance_mutex_handle = None


# ============================================================
# Firebase 與 UI 之間的跨執行緒訊號
# ============================================================


class FirebaseBridge(QObject):
    message_received = Signal(dict)
    status_changed = Signal(str, bool)
    submission_finished = Signal(bool, str)


# ============================================================
# 透明桌面彈幕
# ============================================================


@dataclass
class DanmakuItem:
    text: str
    x: float
    baseline_y: float
    speed: float
    font_size: int
    color: QColor
    width: int


class ScreenOverlayWindow(QWidget):
    """一顆螢幕對應一個透明視窗；動畫座標由 OverlayManager 統一管理。"""

    def __init__(self, screen: Any, item_provider: Any) -> None:
        super().__init__()

        self.screen = screen
        self.item_provider = item_provider

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._update_screen_geometry()
        self.screen.availableGeometryChanged.connect(
            self._update_screen_geometry
        )
        self.screen.geometryChanged.connect(self._update_screen_geometry)
        self.show()

    def _update_screen_geometry(self, *_args: Any) -> None:
        self.setGeometry(self.screen.availableGeometry())
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Source
        )
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        screen_rect = self.screen.availableGeometry()
        screen_left = screen_rect.left()
        screen_top = screen_rect.top()
        screen_right = screen_rect.right()
        screen_bottom = screen_rect.bottom()

        for item in self.item_provider():
            item_left = item.x
            item_right = item.x + item.width
            item_top = item.baseline_y - item.font_size - 8
            item_bottom = item.baseline_y + 8

            if (
                item_right < screen_left
                or item_left > screen_right
                or item_bottom < screen_top
                or item_top > screen_bottom
            ):
                continue

            local_x = item.x - screen_left
            local_baseline_y = item.baseline_y - screen_top
            font = QFont(
                "Microsoft JhengHei",
                item.font_size,
                QFont.Weight.Bold,
            )

            path = QPainterPath()
            path.addText(
                QPointF(local_x, local_baseline_y),
                font,
                item.text,
            )

            painter.setPen(
                QPen(
                    QColor(0, 0, 0, 185),
                    1.5,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(item.color)
            painter.drawPath(path)

        painter.end()


class OverlayManager(QObject):
    """管理所有螢幕與全域彈幕動畫。"""

    def __init__(self) -> None:
        super().__init__()

        application = QApplication.instance()
        if application is None:
            raise RuntimeError("QApplication 尚未建立")

        self.application = application
        self.windows: dict[int, ScreenOverlayWindow] = {}
        self.items: list[DanmakuItem] = []
        self.overlay_enabled = True
        self.last_frame_time = time.monotonic()

        for screen in QApplication.screens():
            self._add_screen(screen)

        self.application.screenAdded.connect(self._on_screen_added)
        self.application.screenRemoved.connect(self._on_screen_removed)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    def _screen_key(self, screen: Any) -> int:
        return id(screen)

    def _add_screen(self, screen: Any) -> None:
        key = self._screen_key(screen)
        if key in self.windows:
            return

        window = ScreenOverlayWindow(screen, lambda: self.items)
        self.windows[key] = window
        if not self.overlay_enabled:
            window.hide()

    def _on_screen_added(self, screen: Any) -> None:
        QTimer.singleShot(150, lambda: self._add_screen(screen))

    def _on_screen_removed(self, screen: Any) -> None:
        window = self.windows.pop(self._screen_key(screen), None)
        if window is not None:
            window.close()
            window.deleteLater()

    def _screen_geometries(self) -> list[Any]:
        return [
            screen.availableGeometry()
            for screen in QApplication.screens()
        ]

    def _desktop_horizontal_bounds(self) -> tuple[int, int]:
        geometries = self._screen_geometries()
        if not geometries:
            return 0, 0

        return (
            min(rect.left() for rect in geometries),
            max(rect.right() for rect in geometries),
        )

    def _shared_vertical_range(self, font_size: int) -> tuple[int, int]:
        geometries = self._screen_geometries()
        if not geometries:
            return font_size + 15, font_size + 16

        margin_top = font_size + 15
        margin_bottom = 35
        overlap_top = max(
            rect.top() + margin_top
            for rect in geometries
        )
        overlap_bottom = min(
            rect.bottom() - margin_bottom
            for rect in geometries
        )

        if overlap_top <= overlap_bottom:
            return overlap_top, overlap_bottom

        rightmost = max(geometries, key=lambda rect: rect.right())
        fallback_top = rightmost.top() + margin_top
        fallback_bottom = max(
            fallback_top + 1,
            rightmost.bottom() - margin_bottom,
        )
        return fallback_top, fallback_bottom

    def add_message(self, text: str) -> None:
        display_text = (
            text.replace("\r\n", " ")
            .replace("\n", " ")
            .strip()
        )
        if not display_text:
            return

        font_size = random.randint(24, 38)
        font = QFont(
            "Microsoft JhengHei",
            font_size,
            QFont.Weight.Bold,
        )
        width = QFontMetrics(font).horizontalAdvance(display_text)
        desktop_left, desktop_right = self._desktop_horizontal_bounds()
        y_top, y_bottom = self._shared_vertical_range(font_size)

        self.items.append(
            DanmakuItem(
                text=display_text,
                # Start just outside the right edge. The animation then lets
                # the first character enter before the rest of the text.
                x=initial_message_x(
                    desktop_left,
                    desktop_right,
                    width,
                ),
                baseline_y=float(random.randint(y_top, y_bottom)),
                speed=float(random.randint(110, 220)),
                font_size=font_size,
                color=random.choice(
                    [
                        QColor("#FFFFFF"),
                        QColor("#FFF200"),
                        QColor("#00E5FF"),
                        QColor("#FFB000"),
                        QColor("#A6FF4D"),
                        QColor("#FF8AD8"),
                    ]
                ),
                width=width,
            )
        )
        self._update_all_windows()

    def clear_messages(self) -> None:
        self.items.clear()
        self._update_all_windows()

    def _tick(self) -> None:
        now = time.monotonic()
        delta_seconds = min(now - self.last_frame_time, 0.1)
        self.last_frame_time = now
        desktop_left, _desktop_right = self._desktop_horizontal_bounds()

        active: list[DanmakuItem] = []
        for item in self.items:
            item.x -= item.speed * delta_seconds
            if item.x + item.width >= desktop_left:
                active.append(item)

        self.items = active
        self._update_all_windows()

    def _update_all_windows(self) -> None:
        for window in list(self.windows.values()):
            window.update()

    def show(self) -> None:
        self.overlay_enabled = True
        for window in list(self.windows.values()):
            window.show()
            window.raise_()

    def hide(self) -> None:
        self.overlay_enabled = False
        for window in list(self.windows.values()):
            window.hide()

    def isVisible(self) -> bool:
        return self.overlay_enabled

    def raise_(self) -> None:
        for window in list(self.windows.values()):
            if window.isVisible():
                window.raise_()

    def close(self) -> None:
        self.timer.stop()
        for window in list(self.windows.values()):
            window.close()
            window.deleteLater()
        self.windows.clear()
        self.items.clear()


# ============================================================
# 系統匣、全域熱鍵與精簡輸入框
# ============================================================

WM_HOTKEY = 0x0312
HOTKEY_ID = 0xD401
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
HOTKEY_VIRTUAL_KEY = ord("D")
HOTKEY_LABEL = "Ctrl+Alt+D"


class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    """接收 Windows RegisterHotKey 產生的 WM_HOTKEY。"""

    def __init__(self, callback: Any) -> None:
        super().__init__()
        self.callback = callback
        self.registered = False

    def register(self) -> bool:
        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        self.registered = bool(
            user32.RegisterHotKey(
                None,
                HOTKEY_ID,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                HOTKEY_VIRTUAL_KEY,
            )
        )
        return self.registered

    def unregister(self) -> None:
        if not self.registered:
            return

        try:
            user32 = ctypes.windll.user32
            user32.UnregisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
            ]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey(None, HOTKEY_ID)
        finally:
            self.registered = False

    def nativeEventFilter(self, event_type: Any, message: Any) -> bool:
        try:
            event_name = bytes(event_type)
        except Exception:
            event_name = b""

        if event_name not in (
            b"windows_dispatcher_MSG",
            b"windows_generic_MSG",
        ):
            return False

        try:
            native_message = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError, OSError):
            return False

        if (
            native_message.message == WM_HOTKEY
            and native_message.wParam == HOTKEY_ID
        ):
            self.callback()
            return True

        return False


class QuickInputWindow(QWidget):
    """Ctrl+Alt+D 投稿輸入介面。"""

    submitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setFixedWidth(600)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #202124;
                color: #f1f3f4;
                font-family: "Microsoft JhengHei";
                font-size: 14px;
            }
            QLineEdit {
                background-color: #303134;
                border: 1px solid #5f6368;
                border-radius: 7px;
                padding: 10px 12px;
                color: #ffffff;
                selection-background-color: #8ab4f8;
            }
            QLineEdit:focus { border: 1px solid #8ab4f8; }
            QPushButton {
                background-color: #8ab4f8;
                color: #202124;
                border: none;
                border-radius: 7px;
                padding: 10px 18px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #aecbfa; }
            QPushButton#closeButton {
                background-color: transparent;
                color: #bdc1c6;
                padding: 4px 8px;
            }
            QPushButton#closeButton:hover {
                background-color: #3c4043;
                color: #ffffff;
            }
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 8, 12, 12)
        root_layout.setSpacing(7)

        header_layout = QHBoxLayout()
        close_button = QPushButton("Esc ×")
        close_button.setObjectName("closeButton")
        close_button.clicked.connect(self.hide)
        header_layout.addStretch(1)
        header_layout.addWidget(close_button)
        root_layout.addLayout(header_layout)

        input_layout = QHBoxLayout()
        input_layout.setSpacing(8)
        self.message_input = QLineEdit()
        self.message_input.setMaxLength(MAX_MESSAGE_LENGTH)
        self.message_input.setPlaceholderText(
            "輸入文字（送到 Firebase，稍後由 Service 1 發到 Discord）"
        )
        self.message_input.returnPressed.connect(self._submit)

        self.send_button = QPushButton("送出")
        self.send_button.clicked.connect(self._submit)
        input_layout.addWidget(self.message_input, 1)
        input_layout.addWidget(self.send_button)
        root_layout.addLayout(input_layout)
        self.adjustSize()

    def show_for_input(self) -> None:
        screen = QApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        self.adjustSize()
        self.move(
            available.left() + (available.width() - self.width()) // 2,
            available.bottom() - self.height() - 32,
        )
        self.show()
        self.raise_()
        self.activateWindow()
        self.message_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.message_input.selectAll()

    def set_busy(self, busy: bool) -> None:
        self.message_input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.send_button.setText("送出中…" if busy else "送出")

    def _submit(self) -> None:
        text = self.message_input.text().strip()
        if text:
            self.submitted.emit(text)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: Any) -> None:
        event.ignore()
        self.hide()


class TrayController(QObject):
    """無常駐主視窗的系統匣控制器。"""

    def __init__(
        self,
        application: QApplication,
        poller: FirebaseFeedClient,
        bridge: FirebaseBridge,
        overlay: OverlayManager,
    ) -> None:
        super().__init__()

        self.application = application
        self.poller = poller
        self.bridge = bridge
        self.overlay = overlay
        self.hotkey_filter: GlobalHotkeyFilter | None = None
        self.connected = False
        self.status_message = "Firebase 尚未連線"
        self.submission_in_flight = False
        self.pending_request_id: str | None = None

        self.seen_message_ids: set[str] = set()
        self.seen_message_order: deque[str] = deque()
        self.max_seen_message_ids = 2000

        icon = self.application.style().standardIcon(
            QStyle.StandardPixmap.SP_ComputerIcon
        )
        self.application.setWindowIcon(icon)

        self.quick_input = QuickInputWindow()
        self.quick_input.submitted.connect(self.send_message)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(f"{APP_TITLE}｜正在啟動")

        self.menu = QMenu()
        self.input_action = self.menu.addAction(
            f"輸入訊息（{HOTKEY_LABEL}；送到 Firebase）"
        )
        self.input_action.triggered.connect(self.show_input)

        self.pause_action = self.menu.addAction("暫停顯示彈幕")
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self.set_overlay_paused)

        self.clear_action = self.menu.addAction("清除目前彈幕")
        self.clear_action.triggered.connect(self.overlay.clear_messages)

        self.menu.addSeparator()
        self.status_action = self.menu.addAction(self.status_message)
        self.status_action.setEnabled(False)
        self.menu.addSeparator()

        self.exit_action = self.menu.addAction("結束程式")
        self.exit_action.triggered.connect(self.shutdown)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        bridge.message_received.connect(self.on_message_received)
        bridge.status_changed.connect(self.on_status_changed)
        bridge.submission_finished.connect(self.on_submission_finished)

    def set_hotkey_filter(self, hotkey_filter: GlobalHotkeyFilter) -> None:
        self.hotkey_filter = hotkey_filter

    def announce_startup(self, hotkey_registered: bool) -> None:
        if hotkey_registered:
            message = (
                f"程式已縮到系統匣。\n"
                f"按 {HOTKEY_LABEL} 輸入文字並投稿到 Firebase。\n"
                "Firebase SSE 會即時接收新彈幕。"
            )
        else:
            message = (
                f"{HOTKEY_LABEL} 已被其他程式占用。\n"
                "請由系統匣選單開啟輸入框。"
            )

        self.tray.showMessage(
            APP_TITLE,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def show_input(self) -> None:
        self.quick_input.show_for_input()

    def send_message(self, text: str) -> None:
        if self.submission_in_flight:
            return
        if self.pending_request_id is None:
            self.pending_request_id = new_request_id()
        request_id = self.pending_request_id
        self.submission_in_flight = True
        self.quick_input.set_busy(True)
        threading.Thread(
            target=self._submit_worker,
            args=(text, request_id),
            daemon=True,
            name="firebase-submit",
        ).start()

    def _submit_worker(self, text: str, request_id: str) -> None:
        try:
            result = submit_text(text, request_id=request_id)
        except SubmissionError as error:
            self.bridge.submission_finished.emit(False, str(error))
            return
        self.bridge.submission_finished.emit(
            True,
            "已送到 Firebase" + ("（重複請求，未新增）" if result.duplicate else ""),
        )

    def on_submission_finished(self, success: bool, message: str) -> None:
        self.submission_in_flight = False
        self.quick_input.set_busy(False)
        if success:
            self.pending_request_id = None
            self.quick_input.message_input.clear()
            self.quick_input.hide()
            return
        self.tray.showMessage(
            "投稿失敗",
            message + "；原文字保留，可再次送出。",
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )

    def on_message_received(self, payload: dict[str, object]) -> None:
        request_id = str(payload.get("request_id", "")).strip()
        if request_id:
            if request_id in self.seen_message_ids:
                return

            self.seen_message_ids.add(request_id)
            self.seen_message_order.append(request_id)
            while len(self.seen_message_order) > self.max_seen_message_ids:
                expired_id = self.seen_message_order.popleft()
                self.seen_message_ids.discard(expired_id)

        text = str(payload.get("text", "")).strip()
        if text:
            self.overlay.add_message(text)

    def on_status_changed(self, message: str, connected: bool) -> None:
        self.connected = connected
        self.status_message = message
        self.status_action.setText(message)
        self.tray.setToolTip(f"{APP_TITLE}｜{message}")
        self.input_action.setEnabled(True)

    def set_overlay_paused(self, paused: bool) -> None:
        if paused:
            self.overlay.hide()
            self.pause_action.setText("恢復顯示彈幕")
        else:
            self.overlay.show()
            self.overlay.raise_()
            self.pause_action.setText("暫停顯示彈幕")

    def on_tray_activated(
        self,
        reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.show_input()

    def shutdown(self) -> None:
        if self.hotkey_filter is not None:
            self.hotkey_filter.unregister()
        self.quick_input.hide()
        self.poller.stop()
        self.overlay.close()
        self.tray.hide()
        self.application.quit()


def main() -> int:
    if sys.platform != "win32":
        print(
            "此程式是 Windows Client，請在 Windows 上執行 "
            "start_danmaku_firebase_py3146.bat。",
            file=sys.stderr,
        )
        return 1

    if not acquire_single_instance():
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("警告：目前 Windows 環境沒有可用的系統匣。")

    bridge = FirebaseBridge()
    poller = FirebaseFeedClient(
        on_message=bridge.message_received.emit,
        on_status=bridge.status_changed.emit,
    )
    overlay = OverlayManager()
    controller = TrayController(app, poller, bridge, overlay)

    hotkey_filter = GlobalHotkeyFilter(controller.show_input)
    controller.set_hotkey_filter(hotkey_filter)
    app.installNativeEventFilter(hotkey_filter)
    hotkey_registered = hotkey_filter.register()

    app.aboutToQuit.connect(hotkey_filter.unregister)
    app.aboutToQuit.connect(poller.stop)
    app.aboutToQuit.connect(release_single_instance)

    poller.start()
    QTimer.singleShot(
        900,
        lambda: controller.announce_startup(hotkey_registered),
    )

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
