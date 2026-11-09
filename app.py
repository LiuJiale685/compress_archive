"""压缩归档工具 - Web 服务 + 桌面壳

架构：
- ThreadingHTTPServer 提供 静态文件 + REST API + SSE
- TaskManager 单任务后台线程，SSE 实时推送日志/进度
- pywebview 可选桌面壳；不可用时自动降级为浏览器打开 localhost

用法：
    python app.py [--gui auto|webview|browser] [--port 0]
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import batch_compress
import batch_decompress
import dedup_clean
import inspect_index

# ==================== 配置 ====================
HERE = os.path.dirname(os.path.abspath(__file__))
TASK_LOG_LIMIT = 2000      # 日志环形缓冲行数
SSE_HEARTBEAT = 15         # SSE 心跳秒数
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# 任务状态机
_STATE = {
    "status": "idle",           # idle / running / finished / failed / stopped
    "type": None,               # compress / clean / decompress
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "log": deque(maxlen=TASK_LOG_LIMIT),  # [ {seq, text}, ... ]
    "seq": 0,                   # 日志全局序号（单调递增）
    "pending": "",              # SSE 端待合并的 end="" 半行
    "pending_seq": None,
    "progress": {"phase": "", "current": None, "total": None, "percent": None},
    "subscribers": set(),       # 每个 SSE 连接一个 queue.Queue
    "stop_event": threading.Event(),
}
_state_lock = threading.Lock()
LAST_DB_PATH = {"path": None}   # 索引页记住上次打开的 db 路径（仅内存）


class TaskBusyError(Exception):
    pass


# ---------------- 日志与进度 ----------------
# progress 解析正则（对应各脚本的日志文案）
_RE_PROG_GROUP = re.compile(r"\[(\d+)/(\d+)\]")            # 压缩/解压 [i/n]
_RE_PROG_HASH = re.compile(r"哈希进度:\s*(\d+)/(\d+)")       # 判重哈希进度
_RE_PROG_HASH_TOTAL = re.compile(r"正在计算哈希进行判重，共 (\d+) 个文件")

_PHASE_KEYS = ["扫描", "判重", "打包", "校验", "重试", "清理", "解压", "完成"]


def _parse_progress(text):
    """从日志行解析进度；返回 (phase|None, current|None, total|None) 或 None"""
    phase = None
    for k in _PHASE_KEYS:
        if k in text:
            phase = k
            break
    m = _RE_PROG_GROUP.search(text)
    if m:
        return phase, int(m.group(1)), int(m.group(2))
    m = _RE_PROG_HASH.search(text)
    if m:
        return phase or "判重", int(m.group(1)), int(m.group(2))
    m = _RE_PROG_HASH_TOTAL.search(text)
    if m:
        return "判重", 0, int(m.group(1))
    return phase, None, None


class TaskManager:
    """单任务调度 + 日志环形缓冲 + SSE 广播"""

    @staticmethod
    def snapshot():
        with _state_lock:
            return _snapshot_locked()

    @staticmethod
    def _log(text, end="\n"):
        with _state_lock:
            if end == "\n":
                # 完整行：若之前有半行积压，与半行内容合并为同一条
                if _STATE["pending"]:
                    seq = _STATE["pending_seq"]
                    _STATE["pending"] = ""
                    _STATE["pending_seq"] = None
                    merged = _STATE["log"][-1]["text"] + text
                    if _STATE["log"] and _STATE["log"][-1]["seq"] == seq:
                        _STATE["log"][-1]["text"] = merged
                    else:
                        _STATE["log"].append({"seq": seq, "text": merged})
                    text = merged
                else:
                    _STATE["seq"] += 1
                    seq = _STATE["seq"]
                    _STATE["log"].append({"seq": seq, "text": text})
            else:  # end == ""
                # 半行：追加到上一序号
                if _STATE["pending"]:
                    _STATE["pending"] += text
                else:
                    _STATE["pending"] = text
                    _STATE["seq"] += 1
                    _STATE["pending_seq"] = _STATE["seq"]
                seq = _STATE["pending_seq"] if _STATE["pending_seq"] else _STATE["seq"]
                _STATE["pending_seq"] = seq
                text = _STATE["pending"]
                _STATE["log"].append({"seq": seq, "text": text})
            # 更新进度
            phase, cur, tot = _parse_progress(text)
            if phase:
                _STATE["progress"]["phase"] = phase
            if cur is not None and tot:
                _STATE["progress"]["current"] = cur
                _STATE["progress"]["total"] = tot
                _STATE["progress"]["percent"] = round(cur / tot * 100, 1)
            log_event = {"seq": seq, "text": text}
            progress_event = dict(_STATE["progress"])
        # 锁外广播，避免 _emit 再次抢锁死锁
        _emit("log", log_event)
        _emit("progress", progress_event)

    @staticmethod
    def submit(task_type, cfg):
        with _state_lock:
            if _STATE["status"] == "running":
                raise TaskBusyError("已有任务在运行")
            _STATE["status"] = "running"
            _STATE["type"] = task_type
            _STATE["started_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            _STATE["finished_at"] = None
            _STATE["result"] = None
            _STATE["error"] = None
            _STATE["progress"] = {"phase": "启动", "current": None, "total": None, "percent": None}
            _STATE["stop_event"] = threading.Event()
            stop_event = _STATE["stop_event"]
            snapshot = _snapshot_locked()
        _emit("task_status", snapshot)
        t = threading.Thread(target=TaskManager._run, args=(task_type, cfg, stop_event), daemon=True)
        t.start()
        return {"task_type": task_type, "status": "running"}

    @staticmethod
    def request_stop():
        with _state_lock:
            if _STATE["status"] != "running":
                return False
            _STATE["stop_event"].set()
            return True

    @staticmethod
    def _run(task_type, cfg, stop_event):
        try:
            log_cb = TaskManager._log
            if task_type == "compress":
                result = batch_compress.run_compress(cfg, log_cb=log_cb, stop_event=stop_event)
                status = "stopped" if stop_event.is_set() else "finished"
            elif task_type == "clean":
                deleted = dedup_clean.dedup_clean(
                    cfg["target_dir"], preview=cfg.get("preview", True),
                    log_cb=log_cb, stop_event=stop_event,
                )
                result = {"deleted": deleted}
                status = "stopped" if stop_event.is_set() else "finished"
            elif task_type == "decompress":
                result = batch_decompress.run_decompress(cfg, log_cb=log_cb, stop_event=stop_event)
                status = "stopped" if stop_event.is_set() else "finished"
            else:
                raise ValueError(f"未知任务类型: {task_type}")
        except batch_compress._TaskStopped:
            status, result, error = "stopped", None, "用户取消"
        except Exception as e:
            status, result, error = "failed", None, f"{type(e).__name__}: {e}"
        else:
            error = None
        with _state_lock:
            _STATE["status"] = status
            _STATE["result"] = result
            _STATE["error"] = error
            _STATE["finished_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            if status in ("finished", "failed", "stopped"):
                _STATE["progress"]["phase"] = "完成" if status == "finished" else ("已停止" if status == "stopped" else "失败")
            snapshot = _snapshot_locked()
        _emit("task_status", snapshot)


def _snapshot_locked():
    """在调用方已持有 _state_lock 时构造状态快照（避免重复加锁死锁）"""
    return {
        "status": _STATE["status"],
        "type": _STATE["type"],
        "started_at": _STATE["started_at"],
        "finished_at": _STATE["finished_at"],
        "result": _STATE["result"],
        "error": _STATE["error"],
        "log": list(_STATE["log"]),
        "progress": dict(_STATE["progress"]),
    }


def _emit(event, data):
    """向所有 SSE 订阅队列广播事件（满则丢最旧）"""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _state_lock:
        for q in list(_STATE["subscribers"]):
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CompressArchive/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # -- 工具 --
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data):
        self._send_json({"ok": True, "data": data})

    def _err(self, message, status=400):
        self._send_json({"ok": False, "error": message}, status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    # -- 路由 --
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/api/"):
                self._handle_api_get(path, query)
            else:
                name = path.lstrip("/")
                if name and os.path.isfile(os.path.join(HERE, name)):
                    self._serve_static(name)
                else:
                    self._send_json({"ok": False, "error": "资源不存在"}, 404)
        except Exception as e:
            self._err(f"服务器错误: {e}", 500)

    def do_POST(self):
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._err(str(e))
            return
        try:
            path = urlparse(self.path).path
            if path == "/api/task/compress":
                self._task_compress(body)
            elif path == "/api/task/clean":
                self._task_clean(body)
            elif path == "/api/task/decompress":
                self._task_decompress(body)
            elif path == "/api/task/stop":
                ok = TaskManager.request_stop()
                self._ok({"requested": ok})
            else:
                self._err("未知接口", 404)
        except TaskBusyError as e:
            self._err(str(e), 409)
        except ValueError as e:
            self._err(str(e))
        except Exception as e:
            self._err(f"服务器错误: {e}", 500)

    # -- 静态文件 --
    def _serve_static(self, name):
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        ext = os.path.splitext(name)[1].lower()
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- API: 任务 --
    def _validate_dir(self, key, value, label=None):
        if not value or not os.path.isdir(value):
            raise ValueError(f"{label or key} 无效或不存在: {value or '(空)'}")
        return os.path.normpath(value)

    def _validate_bz(self, bandizip_dir):
        if not bandizip_dir:
            bandizip_dir = batch_compress.DEFAULT_BANDIZIP_DIR
        if not batch_compress.find_bz_exe(bandizip_dir):
            raise ValueError(f"未找到 Bandizip，请确认安装目录: {bandizip_dir}")
        return bandizip_dir

    def _task_compress(self, body):
        target_dir = self._validate_dir("target_dir", body.get("target_dir"), "目标目录")
        bandizip_dir = self._validate_bz(body.get("bandizip_dir"))
        cfg = {
            "target_dir": target_dir,
            "bandizip_dir": bandizip_dir,
            "password": str(body.get("password") or ""),
            "max_package_size": int(float(body.get("max_package_size_gb") or 3.9) * 1024 ** 3),
            "max_retry": int(body.get("max_retry") or 3),
            "verify_workers": int(body.get("verify_workers") or batch_compress.DEFAULT_VERIFY_WORKERS),
            "hash_workers": int(body.get("hash_workers") or batch_compress.DEFAULT_HASH_WORKERS),
            "enable_hash_index": bool(body.get("enable_hash_index", True)),
            "delete_source": bool(body.get("delete_source", True)),
        }
        self._ok(TaskManager.submit("compress", cfg))

    def _task_clean(self, body):
        target_dir = self._validate_dir("target_dir", body.get("target_dir"), "目标目录")
        cfg = {"target_dir": target_dir, "preview": bool(body.get("preview", True))}
        self._ok(TaskManager.submit("clean", cfg))

    def _task_decompress(self, body):
        source_dir = self._validate_dir("source_dir", body.get("source_dir"), "源目录")
        bandizip_dir = self._validate_bz(body.get("bandizip_dir"))
        output_dir = body.get("output_dir") or os.path.dirname(source_dir)
        cfg = {
            "source_dir": source_dir,
            "output_dir": output_dir,
            "bandizip_dir": bandizip_dir,
            "password": str(body.get("password") or ""),
            "max_retry": int(body.get("max_retry") or 3),
            "delete_after_extract": bool(body.get("delete_after_extract", True)),
        }
        self._ok(TaskManager.submit("decompress", cfg))

    # -- API: 索引 --
    def _db(self, query):
        db = (query.get("db") or [None])[0] or LAST_DB_PATH["path"]
        if not db:
            raise ValueError("未指定索引库，请先选择 .db 文件")
        conn = inspect_index.open_db(db)
        if conn is None:
            raise ValueError(f"索引库不存在: {db}")
        LAST_DB_PATH["path"] = db
        return conn

    @staticmethod
    def _fin(conn):
        if conn is not None:
            conn.close()

    def _handle_api_get(self, path, query):
        if path == "/api/state":
            self._ok(TaskManager.snapshot())
        elif path == "/api/config/defaults":
            self._ok({
                "verify_workers": batch_compress.DEFAULT_VERIFY_WORKERS,
                "hash_workers": batch_compress.DEFAULT_HASH_WORKERS,
            })
        elif path == "/api/events":
            self._sse(query)
        elif path == "/api/index/summary":
            conn = self._db(query)
            try:
                self._ok(inspect_index.get_summary(conn))
            finally:
                self._fin(conn)
        elif path == "/api/index/archives":
            conn = self._db(query)
            try:
                self._ok(inspect_index.get_archives(conn))
            finally:
                self._fin(conn)
        elif path == "/api/index/records":
            conn = self._db(query)
            try:
                self._ok(inspect_index.query_records(
                    conn,
                    filter_archive=(query.get("filter_archive") or [None])[0],
                    filter_path=(query.get("filter_path") or [None])[0],
                    filter_hash=(query.get("filter_hash") or [None])[0],
                    min_size=int((query.get("min_size") or ["0"])[0] or 0) or None,
                    sort=(query.get("sort") or ["path"])[0],
                    limit=int((query.get("limit") or ["100"])[0]),
                    offset=int((query.get("offset") or ["0"])[0]),
                ))
            finally:
                self._fin(conn)
        elif path == "/api/index/duplicates":
            conn = self._db(query)
            try:
                self._ok(inspect_index.get_duplicates(conn))
            finally:
                self._fin(conn)
        else:
            self._err("未知接口", 404)

    # -- SSE --
    def _sse(self, query):
        since = int((query.get("since") or ["0"])[0] or 0)
        q = queue.Queue(maxsize=500)
        with _state_lock:
            _STATE["subscribers"].add(q)
            log_list = list(_STATE["log"])
        snapshot = TaskManager.snapshot()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # 回放 since 之后的日志
            for item in log_list:
                if item["seq"] > since:
                    self.wfile.write(f"event: log\ndata: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            self.wfile.write(f"event: task_status\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=SSE_HEARTBEAT)
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(": ping\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _state_lock:
                _STATE["subscribers"].discard(q)


class IndexApi:
    """pywebview js_api：目录/文件选择。仅 webview 模式可用。"""

    def pick_directory(self, title="选择目录"):
        import webview
        try:
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, directory_dialog=True, allow_multiple=False
            )
            return result[0] if result else None
        except Exception:
            return None

    def pick_file(self, title="选择文件"):
        import webview
        try:
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, directory_dialog=False, allow_multiple=False
            )
            return result[0] if result else None
        except Exception:
            return None


def start_server(host="127.0.0.1", port=0):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.server_address[1]


def main():
    parser = argparse.ArgumentParser(description="压缩归档工具 - Web 界面")
    parser.add_argument("--gui", choices=["auto", "webview", "browser"], default="auto",
                        help="GUI 模式: auto(默认) 尝试 pywebview，失败降级浏览器; webview 强制桌面窗口; browser 强制浏览器")
    parser.add_argument("--port", type=int, default=0, help="服务端口（默认 0 = 动态分配）")
    args = parser.parse_args()

    server, port = start_server(port=args.port)
    url = f"http://127.0.0.1:{port}/"

    def open_browser():
        # 延迟到服务器就绪后打开
        webbrowser.open(url)

    if args.gui == "browser":
        print(f"[app] 浏览器模式: {url}")
        threading.Timer(0.5, open_browser).start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown(server)
        return

    # webview / auto
    try:
        import webview
        print(f"[app] 桌面窗口模式: {url}")
        window = webview.create_window(
            "压缩归档工具",
            url,
            js_api=IndexApi(),
            width=1100,
            height=800,
            min_size=(900, 640),
        )
        webview.start()
    except Exception as e:
        print(f"[app] pywebview 不可用（{e}），已降级为浏览器模式: {url}")
        try:
            open_browser()
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown(server)
        return

    # 窗口关闭后清理
    _shutdown(server)


def _shutdown(server):
    with _state_lock:
        if _STATE["status"] == "running":
            _STATE["stop_event"].set()
            print("[app] 正在停止任务...")
    threading.Thread(target=server.shutdown, daemon=True).start()


if __name__ == "__main__":
    main()
