#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeetingScribe 桌面客户端入口
- 启动后端服务：优先运行 exe 旁的外置 backend/server.py（便于免重打包更新），
  没有则用打包内置的 server 模块兜底
- 用 Edge/Chrome 应用模式打开独立窗口（无浏览器边框，像原生应用）
- 前端页面心跳看门狗：窗口关闭后自动停止服务
"""
import os
import runpy
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

PORT = 7100
APP_URL = f"http://localhost:{PORT}/"

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
_log = open(LOG_DIR / "app.log", "a", encoding="utf-8", buffering=1)
sys.stdout = _log
sys.stderr = _log

# server.py 以 __main__ 方式运行，Server 实例通过这里回传
_box = {}


def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def start_backend():
    os.chdir(ROOT)
    sys.argv = ["server.py", "--port", str(PORT)]
    external = ROOT / "backend" / "server.py"
    if external.exists():
        print(f"使用外置后端: {external}", flush=True)
        # 以非 __main__ 方式加载，拿到模块命名空间后立即返回，再在子线程里跑 main()
        g = runpy.run_path(str(external))
        _box["globals"] = g
        threading.Thread(target=g["main"], daemon=True).start()
        return
    print("外置 backend/server.py 不存在，使用内置模块", flush=True)
    import server as meeting_server  # PyInstaller 通过 --paths backend 静态打包
    threading.Thread(target=meeting_server.main, daemon=True).start()
    return None


def msg_box(text, title="MeetingScribe", error=False):
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)


def find_browser():
    """优先 Edge（Win10/11 自带），其次 Chrome。
    环境变量在某些启动方式下可能缺失，因此同时查注册表 App Paths 和常见固定路径。"""
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        base = os.environ.get(env)
        if base:
            candidates += [
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
    candidates += [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ]
    try:
        import winreg
        for exe in ("msedge.exe", "chrome.exe"):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
                    ) as k:
                        candidates.append(Path(winreg.QueryValue(k, None)))
                except OSError:
                    pass
    except Exception:
        pass
    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except OSError:
            continue
    return None


def open_app_window():
    browser = find_browser()
    print(f"浏览器: {browser or '未找到，使用系统默认'}", flush=True)
    if browser:
        # 独立 user-data-dir：强制新进程实例。
        # 否则 Edge/Chrome 已在运行时会把 --app 参数转发给已有进程并立即退出。
        profile = ROOT / "browser-profile"
        profile.mkdir(exist_ok=True)
        return subprocess.Popen([
            browser,
            f"--app={APP_URL}",
            f"--user-data-dir={profile}",
            "--window-size=1280,820",
            "--disable-session-crashed-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ])
    import webbrowser
    webbrowser.open(APP_URL)
    return None


def http_json(path, timeout=2):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
            import json
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def watchdog_http():
    """纯 HTTP 心跳看门狗，与进程内对象解耦：
    - 等页面首次心跳（最多 180 秒）
    - 之后心跳中断 25 秒视为窗口已关闭
    - 服务本身不可达（意外退出）也立即返回
    返回 True 表示确认窗口生命周期结束；False 表示一直没等到页面（需兜底保活）。"""
    t0 = time.time()
    while time.time() - t0 < 180:
        d = http_json("/api/heartbeat")
        if d is None:
            return True          # 服务没了
        if d.get("last"):
            break
        time.sleep(2)
    else:
        return False
    print("页面已连接，进入心跳监控", flush=True)
    while True:
        time.sleep(3)
        d = http_json("/api/heartbeat")
        if d is None:
            return True          # 服务没了
        last = d.get("last")
        if last and time.time() - last > 25:
            print("窗口已关闭（心跳中断），停止服务", flush=True)
            return True


class SaveApi:
    """前端 js_api：下载文件时弹出 Windows 保存对话框，服务端流式写出，
    避免大录音文件经 base64 穿过 JS 桥"""

    def save_file(self, url_path, filename=""):
        import re
        import shutil
        import urllib.parse

        import webview
        if not webview.windows:
            return "ERROR:窗口不可用"
        url = f"http://127.0.0.1:{PORT}{url_path}"
        try:
            resp = urllib.request.urlopen(url, timeout=60)
        except Exception as e:
            return f"ERROR:下载失败 {e}"
        if not filename:
            cd = resp.headers.get("Content-Disposition", "")
            m = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd, re.I)
            filename = urllib.parse.unquote(m.group(1).rstrip('"')) if m else "download"
        try:
            res = webview.windows[0].create_file_dialog(
                webview.FileDialog.SAVE, save_filename=filename)
        except Exception as e:
            return f"ERROR:保存对话框失败 {e}"
        if not res:
            return ""  # 用户取消
        path = res if isinstance(res, str) else res[0]
        try:
            with open(path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:
            return f"ERROR:写入失败 {e}"
        return path


    def toggle_fullscreen(self):
        """前端全屏按钮：原生窗口全屏（WebView2 宿主不支持网页 Fullscreen API）"""
        import webview
        if webview.windows:
            webview.windows[0].toggle_fullscreen()


def run_webview():
    """优先用 WebView2 原生窗口（参考 DocSearch）：窗口生命周期由系统托管，
    关闭窗口即返回，无需心跳兜底。环境不支持时返回 False 走浏览器方案。"""
    try:
        import webview
    except Exception as e:
        print(f"pywebview 不可用（{e}），回退到浏览器窗口", flush=True)
        return False
    try:
        webview.create_window("会议转写 MeetingScribe", APP_URL,
                              width=1280, height=900, min_size=(800, 600),
                              js_api=SaveApi())
        webview.start(gui="edgechromium", icon=str(ROOT / "assets" / "icon.ico"))
        return True
    except Exception as e:
        print(f"WebView2 启动失败（{e}），回退到浏览器窗口", flush=True)
        return False


def main():
    print(f"[{time.strftime('%F %T')}] MeetingScribe 客户端启动", flush=True)
    started_here = False
    if not server_up():
        start_backend()
        started_here = True
        for _ in range(90):
            if server_up():
                break
            time.sleep(1)
        else:
            msg_box("服务启动失败，请查看 logs\\app.log", error=True)
            return 1

    if run_webview():
        # 窗口已关闭
        if started_here:
            print("窗口已关闭，停止服务", flush=True)
        return 0

    # ---- 浏览器兜底方案：Edge/Chrome 应用模式 + 前端心跳看门狗 ----
    proc = open_app_window()
    if started_here:
        if watchdog_http():
            return 0
        print("长时间未检测到页面，转入对话框保活", flush=True)
    if proc is not None:
        proc.wait()
    else:
        msg_box(f"会议实时转写服务运行中:{APP_URL}\n如窗口未打开请手动访问该地址。\n关闭此对话框将停止服务。")
    if started_here:
        print("窗口已关闭，停止服务", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
