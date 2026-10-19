#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立播放器（atv-player 完整播放界面版）。

复用 atv-player 真正的 PlayerWindow（底部控制条 + 右键菜单），不重写界面。
启动方式照抄文件浏览双击的传参链：
    打开文件/URL -> PlayItem + VodItem + OpenPlayerRequest
    -> PlayerController.create_session -> PlayerWindow.open_session(session)

本地文件：PlayItem.url = 绝对路径，playback_loader=None。
URL：PlayItem.url = 该 URL，playback_loader=None。

运行（需在 atv-player 源码目录下，让 import atv_player 能找到源码）：
    cd ~/src/atv-player
    uv run python /Users/sunrise/atv-player-patches/standalone_player_atv.py [文件路径或URL]

依赖：~/mpv-dist/lib/libmpv.2.dylib（自带完整依赖，含 AVS2/AVS3）。
"""

import ctypes
import ctypes.util
import os
import re
import sys

# --- 通用签名链接 get（不针对某个网盘/后端）---
# 有签名的链接（?sign= / ?token= / 预签名 URL 等）常 302 到存储/CDN，
# httpx 跟绝对地址重定向时不带原查询串，签名就丢了，导致 403/防盗链拦截。
# 这里手动跟重定向：把原 URL 的查询串合并进重定向目标（目标自带的参数优先），
# 并带浏览器 User-Agent（部分服务器拒绝非浏览器 UA 的签名请求）。
# 适用：BlurayIsoInspector（解析 ISO）+ LocalHlsProxyServer（代理取流）。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _signed_url_get(url: str, headers: dict | None = None, **kwargs):
    """通用签名链接 get。

    - 手动跟重定向：同 host 才合并原查询串；跨 host（源站 -> CDN/存储）用目标链接原样，
      签名按 host 隔离，合并会让目标的签名校验失败。
    - 带浏览器 User-Agent + URL 自身 Origin 作 Referer（满足防盗链检查）。
    - 小 Range 扩到 1MB 再发：有些 CDN 对 tiny Range（如 bytes=0-0）回 200 而不是 206，
      对 >=1MB 的 Range 才回 206；扩完再把响应裁回调用方要的那段、重写成 206。
    - 流式读：服务端回 200（忽略 Range）时不读 body，避免把整文件读进内存。
    """
    import httpx
    from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", _BROWSER_UA)
    if "Referer" not in req_headers and "referer" not in req_headers:
        parsed_origin = urlparse(url)
        if parsed_origin.scheme and parsed_origin.netloc:
            req_headers["Referer"] = f"{parsed_origin.scheme}://{parsed_origin.netloc}/"
    timeout = kwargs.get("timeout", 15.0)
    if kwargs.get("follow_redirects") is False:
        status, resp_headers, content = _signed_url_fetch_once(url, req_headers, timeout, None)
        return httpx.Response(
            status, headers=resp_headers, content=content,
            request=httpx.Request("GET", url),
        )

    # 解析调用方要的 Range（若有），小 Range 扩到 1MB
    range_header = req_headers.get("Range") or req_headers.get("range") or ""
    orig_start = orig_end = None
    expanded = False
    range_match = re.match(r"bytes=(\d+)-(\d*)", str(range_header).strip())
    if range_match:
        orig_start = int(range_match.group(1))
        orig_end = int(range_match.group(2)) if range_match.group(2) else None
        if orig_end is not None and (orig_end - orig_start + 1) < 1024 * 1024:
            expanded_end = orig_start + 1024 * 1024 - 1
            req_headers["Range"] = f"bytes={orig_start}-{expanded_end}"
            expanded = True

    original_query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    current_url = url
    for _ in range(11):
        status, resp_headers, content = _signed_url_fetch_once(
            current_url, req_headers, timeout,
            (orig_start, orig_end) if expanded else None,
        )
        if status not in (301, 302, 303, 307, 308):
            if status >= 400:
                print(
                    f"[signed-url] {status} {current_url[:200]}",
                    file=sys.stderr,
                    flush=True,
                )
            return httpx.Response(
                status, headers=resp_headers, content=content,
                request=httpx.Request("GET", current_url),
            )
        location = resp_headers.get("Location", "") or resp_headers.get("location", "")
        if not location:
            return httpx.Response(
                status, headers=resp_headers, content=content,
                request=httpx.Request("GET", current_url),
            )
        target = urljoin(current_url, location)
        parsed = urlparse(target)
        if parsed.netloc == urlparse(current_url).netloc:
            target_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            merged = {**original_query, **target_query}
            current_url = urlunparse(parsed._replace(query=urlencode(merged)))
        else:
            current_url = target
            original_query = {}
    status, resp_headers, content = _signed_url_fetch_once(
        current_url, req_headers, timeout,
        (orig_start, orig_end) if expanded else None,
    )
    return httpx.Response(
        status, headers=resp_headers, content=content,
        request=httpx.Request("GET", current_url),
    )


def _signed_url_fetch_once(url, req_headers, timeout, expanded_range):
    """单次 GET（流式，不跟重定向）。返回 (status, headers, content)。

    expanded_range=(orig_start, orig_end)：若服务端回 206，把内容裁回原 Range、重写 Content-Range；
    若回 200（忽略 Range），不读 body、返回空内容（调用方见非 206 会报 Range 不支持，避免 OOM）。
    """
    import httpx

    with httpx.stream("GET", url, headers=req_headers, timeout=timeout, follow_redirects=False) as resp:
        status = int(resp.status_code)
        resp_headers = dict(resp.headers)
        if status in (301, 302, 303, 307, 308):
            return status, resp_headers, b""
        if status == 206 and expanded_range is not None:
            orig_start, orig_end = expanded_range
            body = resp.read()
            total = None
            cr = resp_headers.get("content-range", "") or resp_headers.get("Content-Range", "")
            cr_match = re.match(r"bytes\s+\d+-\d+/(\d+)", str(cr).strip(), re.IGNORECASE)
            if cr_match:
                total = cr_match.group(1)
            orig_len = orig_end - orig_start + 1
            sliced = body[:orig_len]
            # 删掉旧的 content-range（忽略大小写），避免新旧两个 Content-Range 并存、
            # httpx 拼成 "a, b" 导致调用方正则对不上。
            new_headers = {k: v for k, v in resp_headers.items() if k.lower() != "content-range"}
            if total is not None:
                new_headers["Content-Range"] = f"bytes {orig_start}-{orig_start + len(sliced) - 1}/{total}"
            else:
                # 服务端没给数字 total：沿用服务端原 Content-Range（调用方会按原逻辑处理）
                if cr:
                    new_headers["Content-Range"] = str(cr)
            new_headers["Content-Length"] = str(len(sliced))
            return 206, new_headers, sliced
        if status == 206:
            return status, resp_headers, resp.read()
        # 200（忽略 Range）/ 错误：不读 body，避免把整文件读进内存
        return status, resp_headers, b""


def _make_m3u8_ad_filter():
    """给 PlayerWindow 的 M3U8AdFilter：ISO 解析 + 代理都用通用签名链接 get。"""
    from atv_player.player.bluray_iso import BlurayIsoInspector, prepare_iso_playback
    from atv_player.player.m3u8_ad_filter import M3U8AdFilter
    from atv_player.proxy.server import LocalHlsProxyServer

    proxy = LocalHlsProxyServer(get=_signed_url_get)
    inspector = BlurayIsoInspector(
        prepare_playback=lambda url, headers: prepare_iso_playback(url, headers, get=_signed_url_get),
    )
    return M3U8AdFilter(proxy_server=proxy, bluray_iso_inspector=inspector, get=_signed_url_get)

# 本地文件 get：把 BlurayIsoInspector / 代理服务器的 HTTP Range 请求映射成读本地文件。
# 返回 httpx.Response 风格的对象（status_code / headers / content / raise_for_status）。
class _LocalFileResponse:
    def __init__(self, status_code: int, headers: dict, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"本地文件读取失败，状态码 {self.status_code}")


def _local_file_get(url: str, headers: dict | None = None, **kwargs) -> _LocalFileResponse:
    # url 是 file://<路径> 或裸路径；BlurayIsoInspector / 代理只用它定位文件。
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme in ("file", ""):
        path = unquote(parsed.path) if parsed.scheme == "file" else url
    else:
        path = url
    if not os.path.isfile(path):
        raise FileNotFoundError(f"本地 ISO 不存在：{path}")
    total = os.path.getsize(path)
    range_header = (headers or {}).get("Range", "") or (headers or {}).get("range", "")
    match = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else total - 1
        end = min(end, total - 1)
    else:
        start, end = 0, total - 1
    if start < 0 or start > end or start >= total:
        return _LocalFileResponse(
            416, {"Content-Range": f"bytes */{total}"}, b""
        )
    with open(path, "rb") as f:
        f.seek(start)
        content = f.read(end - start + 1)
    return _LocalFileResponse(
        206,
        {"Content-Range": f"bytes {start}-{end}/{total}", "Content-Length": str(len(content))},
        content,
    )


_local_iso_proxy = None  # LocalHlsProxyServer（单例），本地 ISO 代理
# 本地 ISO 代理 URL -> .clpi 流语言（(kind, lang) 列表）。PlayerWindow 不经 M3U8AdFilter，
# open_paths 在 open_session 后用它给 MpvWidget 补语言标签。
_LOCAL_ISO_LANGUAGES: dict[str, tuple[tuple[str, str], ...]] = {}


def _local_iso_proxy_url(iso_path: str) -> str:
    """本地 .iso 经 BlurayIsoInspector 解析 + LocalHlsProxyServer 代理，返回 /iso/… 代理 URL。"""
    global _local_iso_proxy
    from atv_player.player.bluray_iso import BlurayIsoInspector, prepare_iso_playback
    from atv_player.proxy.server import LocalHlsProxyServer

    if _local_iso_proxy is None:
        _local_iso_proxy = LocalHlsProxyServer(get=_local_file_get)
        _local_iso_proxy.start()
    inspector = BlurayIsoInspector(
        prepare_playback=lambda url, headers: prepare_iso_playback(url, headers, get=_local_file_get),
    )
    file_url = "file://" + iso_path
    plan = inspector.prepare_playback(file_url, {})
    languages = tuple(getattr(plan, "stream_languages", ()) or ())
    segments = tuple(getattr(plan, "playlist_segments", ()) or ())
    if segments:
        proxy_url = _local_iso_proxy.create_iso_playlist_url(
            file_url, headers={}, segments=segments
        )
    else:
        proxy_url = _local_iso_proxy.create_iso_media_url(
            file_url,
            headers={},
            stream_path=plan.stream.path,
            stream_size=plan.stream.size,
            iso_stream_source=plan.source,
        )
    _LOCAL_ISO_LANGUAGES[proxy_url] = languages
    return proxy_url

# --- mpv 要求 LC_NUMERIC=C（Qt 会把它改成 zh_CN.UTF-8，导致 mpv_create 返回 NULL）---
os.environ["LC_NUMERIC"] = "C"

# --- libmpv：劫持 find_library，让 python-mpv 找到 libmpv ---
# 完整打包（PyInstaller）时优先用包内 libmpv（Contents/Frameworks/ 或 _MEIPASS），
# 找不到再回 ~/mpv-dist（源码/启动器模式）。
def _find_mpv_dylib() -> str:
    candidates = []
    # PyInstaller onefile/onedir：_MEIPASS 下的 libmpv
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "libmpv.2.dylib"))
    # macOS .app bundle：Contents/Frameworks/libmpv.2.dylib
    exe = sys.executable
    if exe and ".app/Contents/" in exe:
        app_contents = exe.split(".app/Contents/")[0] + ".app/Contents"
        candidates.append(os.path.join(app_contents, "Frameworks", "libmpv.2.dylib"))
        candidates.append(os.path.join(app_contents, "Resources", "libmpv.2.dylib"))
    candidates.append("/Users/sunrise/mpv-dist/lib/libmpv.2.dylib")
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return candidates[-1]

_MPV_DYLIB = _find_mpv_dylib()

_orig_find_library = ctypes.util.find_library


def _patched_find_library(name):
    if name == "mpv":
        return _MPV_DYLIB
    return _orig_find_library(name)


ctypes.util.find_library = _patched_find_library

# 预加载 libmpv（RTLD_GLOBAL），让其依赖符号对后续加载可见
try:
    ctypes.CDLL(_MPV_DYLIB, mode=ctypes.RTLD_GLOBAL)
except OSError:
    pass  # import mpv 时会再报，错误信息更明确

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QObject

from atv_player.controllers.player_controller import PlayerController
from atv_player.models import AppConfig, PlayItem, VodItem
from atv_player.ui.player_window import PlayerWindow


class _StubApiClient:
    """PlayerController 需要一个 api_client。单机版不读历史，
    create_session 时传 use_local_history=False，不会调 get_history。
    这里留一个返回 None 的存根，以防万一。"""

    def get_history(self, vod_id):
        return None


def _is_url(text: str) -> bool:
    low = text.lower()
    return (
        low.startswith("http://")
        or low.startswith("https://")
        or low.startswith("file://")
        or low.startswith("bluray://")
    )


def _make_play_item(path_or_url: str) -> PlayItem:
    if _is_url(path_or_url):
        # 直接粘的 bluray://<路径>：标题用文件名，path 用解码后的文件路径，url 原样保留。
        if path_or_url.lower().startswith("bluray://"):
            from urllib.parse import unquote, urlparse

            decoded_path = unquote(urlparse(path_or_url).path)
            return PlayItem(
                title=os.path.basename(decoded_path) or path_or_url,
                url=path_or_url,
                path=decoded_path,
            )
        return PlayItem(title=path_or_url, url=path_or_url, path=path_or_url)
    abs_path = os.path.abspath(path_or_url)
    if abs_path.lower().endswith(".iso"):
        # 本地 .iso：源码 BlurayIsoInspector 解析 + LocalHlsProxyServer 出 /iso/… 代理 URL。
        # path 保留原始 ISO 路径（供显示 / 驱动路径判断）。
        proxy_url = _local_iso_proxy_url(abs_path)
        return PlayItem(title=os.path.basename(abs_path), url=proxy_url, path=abs_path)
    return PlayItem(title=os.path.basename(abs_path), url=abs_path, path=abs_path)


def _make_vod(path_or_url: str) -> VodItem:
    if _is_url(path_or_url):
        return VodItem(vod_id=path_or_url, vod_name=path_or_url)
    abs_path = os.path.abspath(path_or_url)
    return VodItem(vod_id=abs_path, vod_name=os.path.basename(abs_path))


def _make_app_icon() -> "QIcon":
    """播放器图标：橙色圆角方块 + 白色播放三角（程序里画，不依赖外部文件）。"""
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QColor as _QColor, QIcon as _QIcon, QPainter as _QPainter, QPixmap as _QPixmap

    size = 512
    pixmap = _QPixmap(size, size)
    pixmap.fill(_Qt.transparent)
    painter = _QPainter(pixmap)
    painter.setRenderHint(_QPainter.Antialiasing)
    # 橙色圆角方块
    painter.setBrush(_QColor("#ff6b1a"))
    painter.setPen(_Qt.NoPen)
    painter.drawRoundedRect(0, 0, size, size, 110, 110)
    # 白色播放三角
    from PySide6.QtGui import QPolygonF as _QPolygonF
    from PySide6.QtCore import QPointF as _QPointF

    triangle = _QPolygonF(
        [
            _QPointF(size * 0.36, size * 0.28),
            _QPointF(size * 0.36, size * 0.72),
            _QPointF(size * 0.72, size * 0.50),
        ]
    )
    painter.setBrush(_QColor("#ffffff"))
    painter.drawPolygon(triangle)
    painter.end()
    return _QIcon(pixmap)


class Launcher(QWidget):
    """启动器：打开文件 / 打开 URL / 最近打开 / 退出。播放时隐藏，播放窗口关闭后重新显示。"""

    def __init__(self, open_callback):
        super().__init__()
        self._open_callback = open_callback
        self._recent: list[str] = []
        self.setWindowTitle("播放器")
        self.setWindowIcon(_make_app_icon())
        self.resize(960, 700)
        self.setStyleSheet(
            "QWidget { font-size: 15px; }"
            "QLabel#section { font-size: 16px; font-weight: bold; margin-top: 8px; }"
            "QPushButton { padding: 12px 10px; border: 1px solid #b0b0b0; border-radius: 8px; background: #f2f2f2; }"
            "QPushButton:hover { background: #e2e2e2; border-color: #808080; }"
            "QPushButton:pressed { background: #d2d2d2; }"
            "QPlainTextEdit { padding: 10px; border-radius: 8px; font-size: 16px; }"
            "QListWidget { border-radius: 8px; padding: 4px; }"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 16, 20, 16)

        # --- URL：多行输入，长 URL 自动换行全显示 ---
        url_label = QLabel("URL", self)
        url_label.setObjectName("section")
        layout.addWidget(url_label)

        self.url_edit = QPlainTextEdit(self)
        self.url_edit.setPlaceholderText("粘贴 URL（http(s)://…），可多行，Ctrl+Enter 播放")
        self.url_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)  # 按窗口宽度换行
        self.url_edit.setFixedHeight(160)
        layout.addWidget(self.url_edit)

        # --- 三个播放按钮放一行：播放 URL | 打开文件（中间）| 剪贴板播放 ---
        play_row = QHBoxLayout()
        self.btn_play_url = QPushButton("播放 URL", self)
        self.btn_play_url.clicked.connect(self._on_play_url)
        play_row.addWidget(self.btn_play_url, 1)

        self.btn_file = QPushButton("打开文件…", self)
        self.btn_file.clicked.connect(self._on_open_file)
        play_row.addWidget(self.btn_file, 1)

        self.btn_paste_play = QPushButton("从剪贴板播放", self)
        self.btn_paste_play.clicked.connect(self._on_paste_play)
        play_row.addWidget(self.btn_paste_play, 1)
        layout.addLayout(play_row)

        # --- 最近打开：双击重播 ---
        recent_label = QLabel("最近打开（双击重播）", self)
        recent_label.setObjectName("section")
        layout.addWidget(recent_label)

        self.recent_list = QListWidget(self)
        self.recent_list.setWordWrap(True)  # 长 URL 在列表里也换行显示
        self.recent_list.itemDoubleClicked.connect(self._on_recent_double_clicked)
        layout.addWidget(self.recent_list, 1)

        self.btn_quit = QPushButton("退出", self)
        self.btn_quit.clicked.connect(QApplication.instance().quit)
        layout.addWidget(self.btn_quit)

    def _remember(self, paths):
        for p in paths:
            if p in self._recent:
                self._recent.remove(p)
            self._recent.insert(0, p)
        self._recent = self._recent[:30]
        self.recent_list.clear()
        self.recent_list.addItems(self._recent)

    def _on_play_url(self):
        url = self.url_edit.toPlainText().strip()
        if not url:
            return
        # 取第一行非空内容当 URL（多行框里可能粘了多余空行）
        url = next((line.strip() for line in url.splitlines() if line.strip()), "")
        if not url:
            return
        self._remember([url])
        self._open_callback([url], 0)

    def _on_paste_play(self):
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        url = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not url:
            return
        self.url_edit.setPlainText(url)
        self._remember([url])
        self._open_callback([url], 0)

    def _on_open_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "打开文件",
            os.path.expanduser("~"),
            "媒体文件 (*.mkv *.mp4 *.avi *.mov *.m2ts *.ts *.m2t *.iso *.mp3 *.flac *.webm *.wmv *.flv *.rmvb *.3gp *.mpg *.mpeg *.vob *.aac *.ogg *.opus);;所有文件 (*)",
        )
        if not files:
            return
        self._remember(files)
        self._open_callback(files, 0)

    def _on_recent_double_clicked(self, item):
        path = item.text()
        if path:
            self._open_callback([path], 0)

    def keyPressEvent(self, event):
        # URL 框里 Ctrl+Enter 直接播放
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            if self.url_edit.hasFocus():
                self._on_play_url()
                return
        super().keyPressEvent(event)


class _SchemeEventFilter(QObject):
    """接 alist scheme（atvplayer://play?url=…）的 QObject 事件过滤器（QFileOpenEvent）。"""

    def __init__(self, open_callback):
        super().__init__()
        self._open_callback = open_callback

    def eventFilter(self, obj, event):
        try:
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.Type.FileOpen:
                # 用 toEncoded() 取原始字节：toString() 会经 QUrl 重序列化，
                # 内层 URL 的 https:// 与 ?sign= 会被拆/截断（之前只剩 h）。
                try:
                    url = bytes(event.url().toEncoded()).decode("utf-8", "replace")
                except Exception:
                    url = event.url().toString()
                if url.startswith("atvplayer://"):
                    # 取 ?url= 之后全部内容（内层 URL 含 ?sign= / & / =，parse_qs 会切碎，不能用）。
                    marker = "?url="
                    if marker in url:
                        target = url.split(marker, 1)[1]
                    elif url.startswith("atvplayer://play/"):
                        target = url[len("atvplayer://play/"):]
                    else:
                        target = ""
                    if target:
                        from urllib.parse import unquote
                        # 全编码（https%3A%2F%2F…）才解一次；已是 http(s):// 的原始链接直接用。
                        if target.startswith(("http%3A", "https%3A", "HTTP%3A", "HTTPS%3A")):
                            target = unquote(target)
                        # open_paths 要列表；传字符串会被按字符拆（首字符 h），故包一层。
                        self._open_callback([target])
                        return True
        except Exception:
            pass
        return super().eventFilter(obj, event)


class StandalonePlayer:
    def __init__(self):
        self.app = QApplication(sys.argv)
        # 应用名/菜单栏显示名改成“播放器”，不再是默认 Python
        self.app.setApplicationName("播放器")
        self.app.setApplicationDisplayName("播放器")
        self.app.setWindowIcon(_make_app_icon())
        # alist 外部播放器 URL scheme（atvplayer://play?url=<编码后链接>）：
        # macOS 经 Apple Event 发给 app，Qt 转成 QFileOpenEvent；这里接住并播。
        # （启动器版 shell 脚本收不到 Apple Event，只有完整版 PyInstaller .app 能用。）
        self._scheme_filter = _SchemeEventFilter(self.open_paths)
        self.app.installEventFilter(self._scheme_filter)
        self.config = AppConfig()
        self.controller = PlayerController(_StubApiClient())
        self.player_window = None
        self.launcher = Launcher(self.open_paths)
        self.launcher.show()

    def _ensure_player_window(self):
        if self.player_window is None:
            self.player_window = PlayerWindow(
                self.controller,
                self.config,
                lambda: None,  # save_config：单机版不存
                m3u8_ad_filter=_make_m3u8_ad_filter(),  # ISO 解析 + 代理用通用签名链接 get
            )
            # 播放窗口关闭 -> 回启动器（照抄 MainWindow 接 closed_to_main 的做法）
            if hasattr(self.player_window, "closed_to_main"):
                try:
                    self.player_window.closed_to_main.connect(self._on_player_closed)
                except Exception:
                    pass
        return self.player_window

    def _on_player_closed(self):
        self.launcher.show()
        self.launcher.raise_()
        self.launcher.activateWindow()

    def open_paths(self, paths, clicked_index=0):
        if not paths:
            return
        try:
            playlist = [_make_play_item(p) for p in paths]
        except Exception as exc:
            QMessageBox.critical(self.launcher, "无法打开", f"打开失败：{exc}")
            return
        vod = _make_vod(paths[clicked_index] if 0 <= clicked_index < len(paths) else paths[0])
        try:
            session = self.controller.create_session(
                vod,
                playlist,
                clicked_index,
                use_local_history=False,
                restore_history=False,
                playback_loader=None,
                source_kind="local",
                source_key="local",
                source_display_name="本地文件",
            )
        except Exception as exc:
            QMessageBox.critical(self.launcher, "无法打开", f"创建播放会话失败：{exc}")
            return
        window = self._ensure_player_window()
        self.launcher.hide()
        try:
            window.open_session(session, start_paused=False)
        except Exception as exc:
            QMessageBox.critical(self.launcher, "无法播放", f"打开播放窗口失败：{exc}")
            self.launcher.show()
            return
        # 本地 ISO：PlayerWindow 不经 M3U8AdFilter，语言到不了 MpvWidget。
        # 这里直接用 _local_iso_proxy_url 存的 .clpi 语言给 MpvWidget 补标签。
        try:
            current_url = playlist[clicked_index].url if 0 <= clicked_index < len(playlist) else ""
            languages = _LOCAL_ISO_LANGUAGES.get(current_url)
            if languages is not None:
                setter = getattr(window.video, "set_iso_track_languages", None)
                if callable(setter):
                    setter(languages)
        except Exception:
            pass
        window.show()
        window.raise_()
        window.activateWindow()

    def run(self):
        # 命令行带一个参数：直接播（文件路径或 URL）
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        if args:
            # 用 QTimer 延迟到事件循环启动后再开，避免在 QApplication 未就绪时弹对话框
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, lambda: self.open_paths(args, 0))
        return self.app.exec()


def main():
    player = StandalonePlayer()
    sys.exit(player.run())


if __name__ == "__main__":
    main()
