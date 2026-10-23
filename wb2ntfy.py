# -*- coding: utf-8 -*-
"""
WorkBuddy -> ntfy 进度推送器（便携版 v0.2）
监听 WorkBuddy 会话记录(JSONL)，把助手的进度文字推送到 ntfy 服务器（支持多个地址）。
图形界面 + 设置持久化（config.json 与本程序同目录）。

新增能力：
- 开机自启动（注册表 Run 项，可在设置里开关）
- 关闭窗口后台运行（最小化到系统托盘，右键托盘可恢复 / 退出）
- 单实例互斥（重复启动会自动恢复已运行的窗口，不会重复推送）
"""
import os
import sys
import json
import time
import queue
import threading
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import urllib.request

try:
    import winreg
except Exception:  # 仅 Windows 有 winreg
    winreg = None

# ---------- 位置 ----------
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
GIF_NAME = "app.gif"
ICO_NAME = "app.ico"

# 隐私要求：默认不携带任何真实 ntfy 地址，首次运行由用户自己填写
DEFAULT_CONFIG = {
    "ntfy_urls": [],
    "watch_dir": os.path.join(os.path.expanduser("~"), ".workbuddy", "projects"),
    "interval": 1.0,
    "max_len": 300,
    "min_len": 4,
    "push_tool_calls": False,
    "title": "WorkBuddy 进度",
    "tags": "robot,workbuddy",
    "autostart": False,
    "close_to_tray": True,
}

# ---------- 设计 token ----------
BG = "#eef1f5"
PANEL = "#ffffff"
ACCENT = "#0e9f4a"
ACCENT_D = "#0b8a3f"
TEXT = "#1f2933"
SUB = "#7a8694"
BORDER = "#e2e6ec"
GREEN = "#0e9f4a"
RED = "#e0533d"

# ---------- Win32 常量 ----------
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_LBUTTONUP = 0x0202
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIF_STATE = 0x8
NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
NIM_SETVERSION = 0x4
NIIF_INFO = 0x1
NOTIFYICON_VERSION_4 = 4
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
IDI_APPLICATION = 32512
SW_RESTROY = 9
TPM_RETURNCMD = 0x0100
TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
MF_STRING = 0x0000
ERROR_ALREADY_EXISTS = 183


def find_gif():
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", APP_DIR)
        p = os.path.join(base, GIF_NAME)
        if os.path.exists(p):
            return p
    p = os.path.join(APP_DIR, GIF_NAME)
    return p if os.path.exists(p) else None


def find_ico():
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", APP_DIR)
        p = os.path.join(base, ICO_NAME)
        if os.path.exists(p):
            return p
    p = os.path.join(APP_DIR, ICO_NAME)
    return p if os.path.exists(p) else None


# ---------- 开机自启动（注册表 Run 项） ----------
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "WorkBuddyNtfyPusher"


def _autostart_target():
    """返回要写入注册表的启动命令。冻结(EXE)直接用自身；脚本模式用 pythonw 跑本文件。"""
    if getattr(sys, "frozen", False):
        return '"%s"' % os.path.abspath(sys.executable)
    py = os.path.abspath(sys.executable)
    # 尽量用 pythonw 避免弹出黑框
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    return '"%s" "%s"' % (py, os.path.abspath(__file__))


def get_autostart():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_autostart(enable):
    if winreg is None:
        return False
    try:
        if enable:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _autostart_target())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
        return True
    except Exception as e:
        print("[autostart] 失败:", e)
        return False


# ---------- 系统托盘（ctypes 直连，零额外依赖） ----------
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),   # 与 uTimeout 共用（union）
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


def _setup_win32():
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    s = ctypes.windll.shell32
    u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    u.RegisterClassExW.restype = wintypes.ATOM
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                  wintypes.HANDLE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_longlong
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.DestroyWindow.restype = wintypes.BOOL
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              wintypes.UINT, wintypes.UINT]
    u.GetMessageW.restype = ctypes.c_int
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.TranslateMessage.restype = wintypes.BOOL
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = ctypes.c_longlong
    u.LoadImageW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.LoadImageW.restype = wintypes.HANDLE
    u.LoadIconW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    u.LoadIconW.restype = wintypes.HANDLE
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.GetCursorPos.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.PostQuitMessage.argtypes = [ctypes.c_int]
    u.PostQuitMessage.restype = None
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                               wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.CreatePopupMenu.argtypes = []
    u.CreatePopupMenu.restype = wintypes.HMENU
    u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                              ctypes.c_ulonglong, wintypes.LPCWSTR]
    u.AppendMenuW.restype = wintypes.BOOL
    u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                 wintypes.LPCVOID]
    u.TrackPopupMenu.restype = wintypes.UINT
    u.DestroyMenu.argtypes = [wintypes.HMENU]
    u.DestroyMenu.restype = wintypes.BOOL
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HANDLE
    s.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                    ctypes.POINTER(NOTIFYICONDATAW)]
    s.Shell_NotifyIconW.restype = wintypes.BOOL


class TrayIcon:
    WM_TRAY = 0x400 + 1
    ID_TRAY = 1

    def __init__(self, tooltip, icon_path=None, on_restore=None, on_quit=None):
        self.tooltip = tooltip
        self.icon_path = icon_path
        self.on_restore = on_restore
        self.on_quit = on_quit
        self.hwnd = None
        self.hicon = None
        self._wndproc = None
        self._thread = None
        self._ready = threading.Event()
        _setup_win32()

    def _wndproc_fn(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_TRAY:
            if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            elif lparam in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                if self.on_restore:
                    self.on_restore()
            return 0
        if msg == WM_DESTROY:
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self):
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(cls)
        WndProcType = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                         wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM)
        self._wndproc = WndProcType(self._wndproc_fn)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "WB2NTFYTrayCls"
        cls.hCursor = ctypes.windll.user32.LoadCursorW(None, 32512)  # IDC_ARROW
        atom = ctypes.windll.user32.RegisterClassExW(ctypes.byref(cls))
        if not atom:
            return False
        self.hwnd = ctypes.windll.user32.CreateWindowExW(
            0, "WB2NTFYTrayCls", None, 0, 0, 0, 0, 0,
            None, None, hinst, None)
        return bool(self.hwnd)

    def _load_icon(self):
        if self.icon_path and os.path.exists(self.icon_path):
            h = ctypes.windll.user32.LoadImageW(
                None, self.icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if h:
                return h
        return ctypes.windll.user32.LoadIconW(
            None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))

    def _add_icon(self):
        self.hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = self.ID_TRAY
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.hIcon = self.hicon
        nid.uCallbackMessage = self.WM_TRAY
        nid.szTip = self.tooltip[:127]
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        nid.uVersion = NOTIFYICON_VERSION_4
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    def _show_menu(self):
        if not self.hwnd:
            return
        hmenu = ctypes.windll.user32.CreatePopupMenu()
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, 1, "显示窗口")
        ctypes.windll.user32.AppendMenuW(hmenu, MF_STRING, 2, "退出")
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetForegroundWindow(self.hwnd)
        cmd = ctypes.windll.user32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_RIGHTALIGN | TPM_BOTTOMALIGN,
            pt.x, pt.y, 0, self.hwnd, None)
        ctypes.windll.user32.DestroyMenu(hmenu)
        ctypes.windll.user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        if cmd == 1 and self.on_restore:
            self.on_restore()
        elif cmd == 2 and self.on_quit:
            self.on_quit()

    def show_balloon(self, title, text):
        if not self.hwnd:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = self.ID_TRAY
        nid.uFlags = NIF_INFO
        nid.szInfo = text[:255]
        nid.szInfoTitle = title[:63]
        nid.uVersion = 10000  # 该字段在 NIF_INFO 时表示显示时长(ms)
        nid.dwInfoFlags = NIIF_INFO
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove(self):
        if self.hwnd:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self.hwnd
            nid.uID = self.ID_TRAY
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ctypes.windll.user32.DestroyWindow(self.hwnd)
        ctypes.windll.user32.PostQuitMessage(0)

    def _run(self):
        if not self._create_window():
            self._ready.set()
            return
        self._add_icon()
        self._ready.set()
        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def wait_ready(self, timeout=5):
        return self._ready.wait(timeout)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # 兼容旧版单地址配置
    urls = [u.strip() for u in (cfg.get("ntfy_urls") or []) if u and u.strip()]
    if not urls and cfg.get("ntfy_url"):
        urls = [cfg["ntfy_url"].strip()]
    cfg["ntfy_urls"] = urls
    cfg.pop("ntfy_url", None)
    if not cfg.get("watch_dir"):
        cfg["watch_dir"] = DEFAULT_CONFIG["watch_dir"]
    # 开机自启动：以注册表实际状态为准
    cfg["autostart"] = bool(get_autostart())
    if "close_to_tray" not in cfg:
        cfg["close_to_tray"] = True
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def send_ntfy(url, text, title, tags):
    clean = url.rstrip("/")
    topic = clean.split("/")[-1]
    base = clean[: clean.rfind("/")] or clean
    if isinstance(tags, str):
        tags = [t for t in tags.replace("，", ",").split(",") if t.strip()]
    payload = json.dumps(
        {"topic": topic, "message": text, "title": title, "tags": tags}
    ).encode("utf-8")
    req = urllib.request.Request(
        base, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def extract_texts(entry, cfg):
    out = []
    if entry.get("type") == "message" and entry.get("role") == "assistant":
        parts = entry.get("content") or []
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "output_text":
                t = (p.get("text") or "").strip()
                if t and not t.startswith("<"):
                    texts.append(t)
        if texts:
            out.append(("msg", "\n".join(texts)))
    elif cfg.get("push_tool_calls") and entry.get("type") == "function_call":
        name = entry.get("name", "?")
        desc = ""
        try:
            args = json.loads(entry.get("arguments") or "{}")
            desc = args.get("description") or args.get("file_path") or args.get("command") or ""
            if isinstance(desc, str):
                desc = desc.strip().splitlines()[0][:120]
        except Exception:
            pass
        if desc:
            out.append(("tool", "%s: %s" % (name, desc)))
    return out


def project_name(path):
    try:
        return os.path.basename(os.path.dirname(path))
    except Exception:
        return ""


def log_daily(url, text, ok=True, err=""):
    """按天写推送日志：logs/YYYY-MM-DD.txt，记录时间 / 结果 / 目标地址 / 内容"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fn = os.path.join(LOG_DIR, time.strftime("%Y-%m-%d") + ".txt")
        with open(fn, "a", encoding="utf-8") as f:
            f.write("%s | %s | %s | %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                "OK" if ok else "FAIL",
                url, text.replace("\n", " ") + ((" | " + err) if err else "")))
    except Exception:
        pass


class Watcher(threading.Thread):
    def __init__(self, cfg, logq):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.logq = logq
        self.stop_flag = threading.Event()
        self.offsets = {}
        self.pushed = set()

    def run(self):
        watch_dir = self.cfg["watch_dir"]
        if not os.path.isdir(watch_dir):
            self.logq.put(("sys", u"监控目录不存在: " + watch_dir))
            return
        self.logq.put(("sys", u"开始监控: " + watch_dir
                       + u"  (推送目标 %d 个)" % len(self.cfg["ntfy_urls"])))
        first = True
        while not self.stop_flag.is_set():
            try:
                files = self._find_files(watch_dir)
                for fp in files:
                    if first:
                        try:
                            self.offsets[fp] = os.path.getsize(fp)
                        except OSError:
                            pass
                        continue
                    self._read_new(fp)
                first = False
            except Exception as e:
                self.logq.put(("sys", u"[错误] " + repr(e)))
            self.stop_flag.wait(max(0.2, float(self.cfg.get("interval", 1.0))))
        self.logq.put(("sys", u"监控已停止"))

    @staticmethod
    def _find_files(watch_dir):
        found = []
        for root, _dirs, names in os.walk(watch_dir):
            for n in names:
                if n.endswith(".jsonl"):
                    found.append(os.path.join(root, n))
        return found

    def _read_new(self, fp):
        try:
            size = os.path.getsize(fp)
        except OSError:
            return
        start = self.offsets.get(fp, 0)
        if size < start:
            start = 0
        if size == start:
            return
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                f.seek(start)
                new_data = f.read()
                self.offsets[fp] = f.tell()
        except OSError as e:
            self.logq.put(("sys", u"[错误] 读取失败 %s: %r" % (fp, e)))
            return
        for line in new_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            mid = entry.get("id")
            for kind, text in extract_texts(entry, self.cfg):
                key = (mid, text) if mid else (fp, text)
                if key in self.pushed:
                    continue
                self.pushed.add(key)
                self._push(fp, kind, text)

    def _push(self, fp, kind, text):
        urls = self.cfg.get("ntfy_urls") or []
        if not urls:
            return
        max_len = int(self.cfg.get("max_len", 300))
        min_len = int(self.cfg.get("min_len", 4))
        if len(text) < min_len:
            return
        if len(text) > max_len:
            text = text[:max_len] + "…"
        title = self.cfg.get("title", "WorkBuddy 进度")
        proj = project_name(fp)
        if proj:
            title = title + " · " + proj
        if kind == "tool":
            text = "🔧 " + text
        ok_all = True
        for url in urls:
            try:
                send_ntfy(url, text, title, self.cfg.get("tags", ""))
                log_daily(url, text, ok=True)
            except Exception as e:
                ok_all = False
                err = repr(e)
                log_daily(url, text, ok=False, err=err)
                self.logq.put(("sys", u"[推送失败] %s : %r" % (url, e)))
        if ok_all:
            self.logq.put(("push", text))
            self.logq.put(("sys", u"已推送到 %d 个地址" % len(urls)))


class Toggle(tk.Label):
    """自定义勾选框：选中 ☑ / 未选 ☐，保证显示对号而不是叉号"""

    def __init__(self, master, text, variable, command=None):
        self._text = text
        self._var = variable
        self._command = command
        super().__init__(master, bg=PANEL, fg=TEXT, anchor="w",
                         font=("Segoe UI Symbol", 10), cursor="hand2")
        self.bind("<Button-1>", self._toggle)
        self._render()

    def _render(self):
        box = u"☑" if self._var.get() else u"☐"
        self.config(text=u"%s  %s" % (box, self._text))

    def _toggle(self, _e=None):
        self._var.set(not self._var.get())
        self._render()
        if self._command:
            self._command()


class App:
    def __init__(self, root):
        self.root = root
        self._dpi()
        root.title(u"WorkBuddy → ntfy 进度推送器")
        root.geometry("490x590")
        root.minsize(460, 530)
        root.configure(bg=BG)
        self.cfg = load_config()
        self.logq = queue.Queue()
        self.watcher = None
        self._style()
        self._icon()
        self._build(root)
        root.after(200, self._poll_log)
        root.protocol("WM_DELETE_WINDOW", self._close)
        # 系统托盘
        self._tray = TrayIcon(
            tooltip=u"WorkBuddy → ntfy 进度推送器",
            icon_path=find_ico(),
            on_restore=lambda: self.root.after(0, self._restore),
            on_quit=lambda: self.root.after(0, self._quit_app),
        )
        self._tray.start()
        self.start()

    def _dpi(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    def _icon(self):
        try:
            g = find_gif()
            if g:
                self.root.iconphoto(True, tk.PhotoImage(file=g))
        except Exception:
            pass

    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 9))
        s.configure("TButton", background=PANEL, foreground=TEXT, borderwidth=1,
                    relief="solid", padding=(12, 5), font=("Segoe UI", 9))
        s.map("TButton", background=[("active", "#e6eaf0")])
        s.configure("Accent.TButton", background=ACCENT, foreground="white",
                    borderwidth=0, relief="flat", padding=(12, 5),
                    font=("Segoe UI", 9, "bold"))
        s.map("Accent.TButton", background=[
            ("active", ACCENT_D), ("disabled", "#b6c2cd")])
        s.configure("TEntry", fieldbackground=PANEL, foreground=TEXT,
                    borderwidth=1, relief="solid", padding=(7, 4),
                    font=("Segoe UI", 9))
        s.configure("TSeparator", background=BORDER)
        s.configure("TLabel", background=PANEL, foreground=TEXT,
                    font=("Segoe UI", 9))

    def _card(self, parent):
        return tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)

    def _build(self, root):
        hdr = tk.Frame(root, bg=ACCENT)
        hdr.pack(fill="x")
        t = tk.Frame(hdr, bg=ACCENT)
        t.pack(side="left", padx=12, pady=8)
        tk.Label(t, text=u"WorkBuddy → ntfy 进度推送器",
                 bg=ACCENT, fg="white", font=("Segoe UI", 12, "bold"),
                 justify="left").pack(anchor="w")
        tk.Label(t, text=u"实时把助手进度推送到手机 · 免安装便携版",
                 bg=ACCENT, fg="#cdeedb", font=("Segoe UI", 8),
                 justify="left").pack(anchor="w")

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        c1 = self._card(body)
        c1.grid(row=0, column=0, sticky="we", pady=(0, 9))
        self._sect(c1, u"设置")
        inner = tk.Frame(c1, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=(2, 10))
        inner.columnconfigure(0, weight=1)

        tk.Label(inner, text=u"ntfy 推送地址（可添加多个，推送时同时发送）",
                 bg=PANEL, fg=SUB, font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 2))
        lbrow = tk.Frame(inner, bg=PANEL)
        lbrow.pack(fill="x")
        self.url_list = tk.Listbox(lbrow, height=3, bg=PANEL, fg=TEXT,
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=BORDER,
                                   font=("Segoe UI", 9),
                                   selectbackground=ACCENT,
                                   selectforeground="white")
        self.url_list.pack(side="left", fill="x", expand=True)
        sb = tk.Scrollbar(lbrow, command=self.url_list.yview)
        sb.pack(side="right", fill="y")
        self.url_list.config(yscrollcommand=sb.set)
        for u in self.cfg.get("ntfy_urls", []):
            self.url_list.insert("end", u)

        addrow = tk.Frame(inner, bg=PANEL)
        addrow.pack(fill="x", pady=(5, 0))
        self.e_new_url = ttk.Entry(addrow)
        self.e_new_url.pack(side="left", fill="x", expand=True)
        self.e_new_url.insert(0, u"http://服务器:端口/主题")
        self.e_new_url.bind("<FocusIn>", self._url_placeholder_off)
        self.e_new_url.bind("<Return>", lambda e: self._add_url())
        ttk.Button(addrow, text=u"添加", width=6,
                   command=self._add_url).pack(side="left", padx=(6, 0))
        ttk.Button(addrow, text=u"删除所选", width=8,
                   command=self._del_url).pack(side="left", padx=(6, 0))

        tk.Label(inner, text=u"监控目录", bg=PANEL, fg=SUB,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 2))
        drow = tk.Frame(inner, bg=PANEL)
        drow.pack(fill="x")
        self.e_dir = ttk.Entry(drow)
        self.e_dir.pack(side="left", fill="x", expand=True)
        self.e_dir.insert(0, self.cfg.get("watch_dir", ""))
        ttk.Button(drow, text=u"浏览…", width=8,
                   command=self._browse).pack(side="left", padx=(6, 0))

        r2 = tk.Frame(inner, bg=PANEL)
        r2.pack(fill="x", pady=(8, 0))
        tk.Label(r2, text=u"轮询(秒)", bg=PANEL, fg=SUB,
                 font=("Segoe UI", 8)).pack(side="left")
        self.e_int = ttk.Entry(r2, width=6)
        self.e_int.pack(side="left", padx=5)
        self.e_int.insert(0, str(self.cfg.get("interval", 1.0)))
        tk.Label(r2, text=u"最大长度", bg=PANEL, fg=SUB,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0))
        self.e_max = ttk.Entry(r2, width=6)
        self.e_max.pack(side="left", padx=5)
        self.e_max.insert(0, str(self.cfg.get("max_len", 300)))

        self.v_tool = tk.BooleanVar(value=bool(self.cfg.get("push_tool_calls")))
        Toggle(inner, u"同时推送工具调用说明（写入/修改了哪个文件）",
               self.v_tool).pack(fill="x", pady=(8, 0))

        # 新增：开机自启动 / 关闭到托盘
        self.v_auto = tk.BooleanVar(value=bool(self.cfg.get("autostart")))
        self.t_auto = Toggle(inner, u"开机自动启动（登录后后台运行）", self.v_auto,
                             command=self._on_auto_toggle)
        self.t_auto.pack(fill="x", pady=(6, 0))
        self.v_tray = tk.BooleanVar(value=bool(self.cfg.get("close_to_tray", True)))
        Toggle(inner, u"关闭窗口时隐藏到托盘后台运行（取消则直接退出）",
               self.v_tray).pack(fill="x", pady=(2, 0))

        c2 = self._card(body)
        c2.grid(row=1, column=0, sticky="we", pady=(0, 9))
        bar = tk.Frame(c2, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=10)
        dot = tk.Frame(bar, width=9, height=9, bg=RED, highlightthickness=0)
        dot.pack(side="left", padx=(0, 7))
        self.dot = dot
        self.status = tk.StringVar(value=u"未启动")
        self.status_lbl = tk.Label(bar, textvariable=self.status,
                                   bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold"))
        self.status_lbl.pack(side="left")
        btns = tk.Frame(bar, bg=PANEL)
        btns.pack(side="right")
        ttk.Button(btns, text=u"保存", command=self.save).pack(side="left", padx=3)
        ttk.Button(btns, text=u"测试", command=self.test).pack(side="left", padx=3)
        self.btn_start = ttk.Button(btns, text=u"启动", command=self.start,
                                    style="Accent.TButton")
        self.btn_start.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(btns, text=u"停止", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=3)

        c3 = self._card(body)
        c3.grid(row=2, column=0, sticky="wens")
        self._sect(c3, u"推送日志（txt 存于程序同目录 logs/ 下，按天一个文件）")
        lg = tk.Frame(c3, bg=PANEL)
        lg.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self.log = scrolledtext.ScrolledText(
            lg, bg=PANEL, fg=TEXT, state="disabled", wrap="word",
            font=("Segoe UI", 9), relief="flat", borderwidth=0,
            highlightthickness=0, height=6)
        self.log.pack(fill="both", expand=True)

    # ---------- URL 列表 ----------
    def _url_placeholder_off(self, _e=None):
        if self.e_new_url.get() == u"http://服务器:端口/主题":
            self.e_new_url.delete(0, "end")

    def _add_url(self):
        u = self.e_new_url.get().strip()
        if not u or u == u"http://服务器:端口/主题":
            return
        if "://" not in u:
            messagebox.showwarning(u"格式提示", u"地址需形如 http://服务器:端口/主题")
            return
        existing = list(self.url_list.get(0, "end"))
        if u in existing:
            messagebox.showinfo(u"已存在", u"该地址已在列表中")
            return
        self.url_list.insert("end", u)
        self.e_new_url.delete(0, "end")

    def _del_url(self):
        sel = self.url_list.curselection()
        for i in reversed(sel):
            self.url_list.delete(i)

    def _get_urls(self):
        return [u.strip() for u in self.url_list.get(0, "end") if u.strip()]

    def _browse(self):
        d = filedialog.askdirectory(
            title=u"选择 WorkBuddy 会话记录目录",
            initialdir=self.e_dir.get().strip() or os.path.expanduser("~"))
        if d:
            self.e_dir.delete(0, "end")
            self.e_dir.insert(0, os.path.normpath(d))

    def _sect(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=ACCENT, anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(
            anchor="w", padx=12, pady=(9, 0))

    def _on_auto_toggle(self):
        ok = set_autostart(self.v_auto.get())
        if not ok:
            messagebox.showwarning(u"开机自启动",
                                    u"写入注册表失败，可能无权限。\n可手动把本程序快捷方式放进“启动”文件夹。")
            self.v_auto.set(not self.v_auto.get())
            self.t_auto._render()

    # ---------- actions ----------
    def _collect(self):
        cfg = dict(self.cfg)
        cfg["ntfy_urls"] = self._get_urls()
        cfg["watch_dir"] = self.e_dir.get().strip()
        try:
            cfg["interval"] = float(self.e_int.get().strip() or "1")
        except ValueError:
            cfg["interval"] = 1.0
        try:
            cfg["max_len"] = int(self.e_max.get().strip() or "300")
        except ValueError:
            cfg["max_len"] = 300
        cfg["push_tool_calls"] = self.v_tool.get()
        cfg["autostart"] = self.v_auto.get()
        cfg["close_to_tray"] = self.v_tray.get()
        return cfg

    def save(self):
        self.cfg = self._collect()
        save_config(self.cfg)
        messagebox.showinfo(u"已保存", u"设置已保存到:\n" + CONFIG_PATH)

    def test(self):
        self.cfg = self._collect()
        urls = self.cfg["ntfy_urls"]
        if not urls:
            messagebox.showwarning(u"提示", u"请先添加至少一个 ntfy 地址")
            return
        errs = 0
        for u in urls:
            try:
                send_ntfy(u, u"测试通知：推送器工作正常 ✅",
                          self.cfg.get("title", "WorkBuddy 进度"),
                          self.cfg.get("tags", ""))
                log_daily(u, u"测试通知", ok=True)
            except Exception as e:
                errs += 1
                log_daily(u, u"测试通知", ok=False, err=repr(e))
        if errs:
            messagebox.showerror(u"发送失败", u"%d 个地址发送失败，详见推送日志" % errs)
        else:
            self.logq.put(("sys", u"测试通知已发送到 %d 个地址" % len(urls)))

    def start(self):
        if self.watcher and self.watcher.is_alive():
            return
        self.cfg = self._collect()
        if not self.cfg["ntfy_urls"]:
            messagebox.showwarning(u"提示", u"请先添加至少一个 ntfy 地址")
            return
        if not os.path.isdir(self.cfg["watch_dir"]):
            messagebox.showerror(u"错误", u"监控目录不存在:\n" + self.cfg["watch_dir"])
            return
        save_config(self.cfg)
        self.watcher = Watcher(self.cfg, self.logq)
        self.watcher.start()
        self._set_status(u"监控中 · %d 个推送地址" % len(self.cfg["ntfy_urls"]), True)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

    def stop(self):
        if self.watcher:
            self.watcher.stop_flag.set()
            self.watcher = None
        self._set_status(u"未启动", False)
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _set_status(self, text, ok):
        self.status.set(text)
        self.dot.config(bg=GREEN if ok else RED)

    def _poll_log(self):
        try:
            while True:
                kind, text = self.logq.get_nowait()
                ts = time.strftime("%H:%M:%S")
                self.log.config(state="normal")
                if kind == "push":
                    self.log.insert("end", u"[%s] ➜ %s\n\n" % (ts, text))
                else:
                    self.log.insert("end", u"[%s] %s\n" % (ts, text))
                self.log.see("end")
                self.log.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(300, self._poll_log)

    # ---------- 窗口关闭 / 托盘 ----------
    def _close(self):
        if self.cfg.get("close_to_tray", True) and self._tray:
            self.root.withdraw()
            try:
                self._tray.wait_ready(3)
                self._tray.show_balloon(
                    u"已在后台运行",
                    u"窗口已最小化到系统托盘，仍在推送进度；右键托盘图标可恢复或退出。")
            except Exception:
                pass
            self.logq.put(("sys", u"已隐藏到系统托盘，后台继续推送"))
        else:
            self._quit_app()

    def _restore(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _quit_app(self):
        self.stop()
        if self._tray:
            try:
                self._tray.wait_ready(3)
                self._tray.remove()
            except Exception:
                pass
        self.root.destroy()


def main():
    # 单实例互斥：重复启动会恢复已运行的窗口，避免重复推送
    mutex_name = "WorkBuddyNtfyPusher_Singleton"
    kernel32 = ctypes.windll.kernel32
    hmutex = kernel32.CreateMutexW(None, True, mutex_name)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, u"WorkBuddy → ntfy 进度推送器")
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTROY)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    finally:
        kernel32.ReleaseMutex(hmutex)
        kernel32.CloseHandle(hmutex)


if __name__ == "__main__":
    main()
