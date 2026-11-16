# -*- coding: utf-8 -*-
"""工作台 VLM 模式：127.0.0.1:8765 静态服务 + /ollama/* 同源转发。

背景：Ollama 默认放行 http://localhost / 127.0.0.1 来源，但拒绝 file://
与内置预览面板 about:// 的 Origin: null（实测 403）。这些页面点「VLM 预标注」
会一律报「连不上」。本脚本把 /ollama/* 原样转发到 127.0.0.1:11434 并加
CORS 头，于是任何来源的页面都能通过 http://127.0.0.1:8765/ollama/* 调用模型。

两种运行方式：
  · 双击 start_vlm_workbench.bat          → 有窗口、自动开浏览器（手动调试用）
  · pythonw start_vlm_workbench.py --silent → 无窗口后台常驻（开机自启用）
    静默模式不打印、不开浏览器、端口已占用时直接退出，可安全重复启动。

特点：不改 OLLAMA_ORIGINS、不重启 Ollama、不装任何东西、纯标准库、
无业务逻辑（只做字节转发），照片只在 127.0.0.1 流转，不上传。
"""
import functools
import http.server
import json
import pathlib
import socketserver
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parent
PORT = 8765
URL = "http://127.0.0.1:%d/index.html" % PORT
OLLAMA_BASE = "http://127.0.0.1:11434"
SILENT = "--silent" in sys.argv


def out(*args):
    if not SILENT:
        print(*args, flush=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + /ollama/* 纯转发（无业务逻辑，不做任何图像处理）。"""

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _proxy(self, method):
        if not self.path.startswith("/ollama/"):
            self.send_error(404)
            return
        target = OLLAMA_BASE + self.path[len("/ollama"):]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(target, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data, status = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data, status, ctype = e.read(), e.code, "application/json"
        except Exception as e:
            data = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            status, ctype = 502, "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/ollama/"):
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/ollama/"):
            return self._proxy("POST")
        self.send_error(405)


Quiet = functools.partial(QuietHandler, directory=str(ROOT))


def preflight():
    """手动模式启动前检查 Ollama 服务与视觉模型，把失败原因讲清楚。"""
    out("  检查本机 Ollama …", end=" ")
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=6) as r:
            o = json.load(r)
        names = [m.get("name", "") for m in o.get("models", [])]
    except Exception as e:
        out("失败")
        out("    连不上 127.0.0.1:11434（%s）。" % e)
        out("    请先在托盘启动 Ollama，或命令行执行 ollama serve。")
        out("    工作台仍会打开，但「VLM 预标注」不可用；其他功能不受影响。")
        return False
    if not any(n.startswith("qwen3-vl") for n in names):
        out("缺模型")
        out("    当前模型：", "、".join(names) or "（空）")
        out("    请执行：ollama pull qwen3-vl:4b")
        return False
    out("OK（%s，共 %d 个模型）" % ("、".join(names)[:60], len(names)))
    return True


if __name__ == "__main__":
    out("=" * 66)
    out("  工作台 · VLM 模式（本机 Ollama 构件预标注）")
    out("=" * 66)
    if not SILENT:
        preflight()
    # 端口被占用：说明已有实例在跑。静默模式直接成功退出（可安全重复启动）
    try:
        probe = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Quiet)
        probe.server_close()
    except OSError as e:
        if SILENT:
            raise SystemExit(0)
        out("  端口 %d 已被占用：%s" % (PORT, e))
        out("  如果之前已经启动过本工作台，直接用浏览器打开 %s" % URL)
        out("  否则关掉占用端口的程序后重试。")
        threading.Timer(1.0, lambda: webbrowser.open(URL)).start()
        input("  按回车键退出…")
        raise SystemExit(1)
    if not SILENT:
        threading.Timer(0.8, lambda: webbrowser.open(URL)).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Quiet) as httpd:
        out("  地址：", URL)
        out("  Ollama 转发：/ollama/* → 127.0.0.1:11434 · 无需跨域白名单")
        out("  保持本服务运行，预览面板 / file:// 页面也能用 VLM")
        out("  按 Ctrl+C 或关闭本窗口即结束；双击 index.html 仍可正常使用")
        out("=" * 66)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            out("\n已关闭。")
