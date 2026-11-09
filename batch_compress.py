import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from queue import Queue

# ==================== 默认配置项 ====================
DEFAULT_TARGET_DIR = r"F:\ll"  # 目标目录默认值，可用命令行参数覆盖
DEFAULT_HASH_INDEX = True  # 是否启用哈希判重与索引入库，默认开启，可用 --no-hash-index 关闭
DEFAULT_DELETE_SOURCE = True  # 压缩校验通过后是否删除源文件，默认删除，可用 --keep-source 关闭
DEFAULT_BANDIZIP_DIR = r"C:\Program Files\Bandizip"
DEFAULT_MAX_PACKAGE_SIZE = int(3.9 * 1024 * 1024 * 1024)
DEFAULT_MAX_RETRY = 3  # 压缩失败最大重试次数
DEFAULT_VERIFY_WORKERS = 2  # 校验线程数（跑 bz t 外部进程，属磁盘 I/O 型，少量即可）
# 哈希计算为纯 CPU 密集任务，BLAKE2b-256 哈希计算线程按 CPU 核数自适应（上限 8 防过杀）
DEFAULT_HASH_WORKERS = min(os.cpu_count() or 4, 8)

# 内部 _TaskStopped 异常：用户请求停止时由核心循环抛出
class _TaskStopped(Exception):
    pass


# 线程安全的打印锁（CLI 模式经 print 输出走此锁）
_print_lock = threading.Lock()
# 共享计数器锁（stats dict 累加用）
_counter_lock = threading.Lock()


def _default_log_cb(text, end="\n"):
    with _print_lock:
        print(text, end=end)


# 当前日志回调，由 run_compress 设置；默认为 print，保证 CLI 兼容
_LOG_CB = _default_log_cb


def safe_print(*args, **kwargs):
    """统一经日志回调输出（text 拼接 + end 透传）"""
    end = kwargs.pop("end", "\n")
    text = " ".join(str(a) for a in args)
    _LOG_CB(text, end=end)


def parse_args(argv=None):
    """解析命令行参数：目标目录（位置参数）+ 哈希入库开关 + 源文件保留开关"""
    parser = argparse.ArgumentParser(
        description="按创建时间分卷批量压缩，并可按需进行哈希判重与索引入库",
    )
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=DEFAULT_TARGET_DIR,
        help=f"待压缩的目标目录（默认: {DEFAULT_TARGET_DIR}）",
    )
    parser.add_argument(
        "--no-hash-index",
        dest="hash_index",
        action="store_false",
        default=DEFAULT_HASH_INDEX,
        help="关闭哈希判重与索引入库（默认开启；关闭后不计算哈希、不读写索引库）",
    )
    parser.add_argument(
        "--keep-source",
        dest="delete_source",
        action="store_false",
        default=DEFAULT_DELETE_SOURCE,
        help="压缩并校验通过后保留源文件，不删除（默认删除源文件）",
    )
    parser.add_argument(
        "--hash-workers",
        type=int,
        default=None,
        help=f"哈希计算线程数（默认按 CPU 核数自适应: {DEFAULT_HASH_WORKERS}）",
    )
    parser.add_argument(
        "--verify-workers",
        type=int,
        default=None,
        help=f"校验（bz t）线程数（默认: {DEFAULT_VERIFY_WORKERS}）",
    )
    return parser.parse_args(argv)


def build_index_db_path(target_dir):
    """索引库路径 = 代码目录 + TARGET_DIR 末层名 + _dedup_index.db（适配多项目）"""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"{os.path.basename(os.path.normpath(target_dir))}_dedup_index.db",
    )


# 哨兵值，用于通知消费者线程退出
_SENTINEL = None


class DedupIndex:
    """SQLite 判重索引：记录每个已打包文件的 路径/大小/mtime/哈希/所在压缩包"""

    def __init__(self, db_path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS packed_files (
                rel_path   TEXT PRIMARY KEY,
                size       INTEGER NOT NULL,
                mtime_ns   INTEGER NOT NULL,
                hash       TEXT NOT NULL,
                archive    TEXT NOT NULL,
                packed_at  TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def load_all(self):
        """加载全部索引到内存: rel_path -> (size, mtime_ns, hash)"""
        with self._lock:
            cur = self._conn.execute("SELECT rel_path, size, mtime_ns, hash FROM packed_files")
            return {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}

    def upsert_many(self, entries, archive):
        """批量写入/覆盖索引。entries: [(rel_path, size, mtime_ns, hash)]"""
        now = datetime.now().isoformat(timespec="seconds")
        rows = [(rel, size, mtime_ns, h, archive, now) for rel, size, mtime_ns, h in entries]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO packed_files"
                " (rel_path, size, mtime_ns, hash, archive, packed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def count(self):
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM packed_files")
            return cur.fetchone()[0]

    def close(self):
        with self._lock:
            self._conn.close()


def compute_hash(path):
    """计算文件的 BLAKE2b-256 哈希"""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_item_size(path):
    """获取文件或目录的大小"""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def get_items_sorted_by_ctime(source_dir, output_dir):
    """收集目录下的文件和子目录（排除输出目录），按创建时间排序"""
    items = []
    output_path = Path(output_dir)
    for entry in os.scandir(source_dir):
        p = Path(entry.path)
        if p == output_path:
            continue
        items.append(p)
    items.sort(key=lambda p: p.stat().st_ctime)
    return items


def dedup_filter(items, index_snapshot, target_dir, hash_workers, log_cb=None):
    """
    判重过滤：
    - 顶层文件按文件级判重；顶层目录按目录级判重（内部文件全部已打包才整目录跳过）
    - 元数据(路径+大小+mtime)与索引一致后再比 BLAKE2b 哈希确认

    返回 (pack_items, item_snapshots, residual_items)
    pack_items:      需要打包的条目（保持原 ctime 顺序）
    item_snapshots:  {item: [(rel_path, size, mtime_ns, hash)]}，校验通过后写索引用
    residual_items:  已确认打包但仍残留在磁盘的条目（调用方负责清理）
    """
    log = log_cb or _LOG_CB
    item_files = {}
    for item in items:
        if item.is_dir():
            item_files[item] = sorted(p for p in item.rglob("*") if p.is_file())
        else:
            item_files[item] = [item]

    file_info = {}      # path -> (rel, size, mtime_ns, expected_hash or None)
    confirm_items = []  # 元数据全部匹配，待哈希确认的 item
    pack_set = set()    # 元数据阶段已确定要打包的 item

    for item, files in item_files.items():
        if not files:
            pack_set.add(item)  # 空目录不做判重，直接打包
            continue
        all_match = True
        for f in files:
            st = f.stat()
            rel = f.relative_to(target_dir).as_posix()
            rec = index_snapshot.get(rel)
            if rec and rec[0] == st.st_size and rec[1] == st.st_mtime_ns:
                file_info[f] = (rel, st.st_size, st.st_mtime_ns, rec[2])
            else:
                file_info[f] = (rel, st.st_size, st.st_mtime_ns, None)
                all_match = False
        if all_match:
            confirm_items.append(item)
        else:
            pack_set.add(item)

    # 并行计算哈希：待打包文件需要哈希写索引，待确认文件需要哈希比对
    files_to_hash = list(file_info.keys())
    total = len(files_to_hash)
    hashes = {}  # path -> hex，失败为 None
    if total:
        log(f"正在计算哈希进行判重，共 {total} 个文件...")
        done = 0
        with ThreadPoolExecutor(max_workers=hash_workers) as pool:
            futures = {pool.submit(compute_hash, f): f for f in files_to_hash}
            for fut in as_completed(futures):
                f = futures[fut]
                try:
                    hashes[f] = fut.result()
                except Exception as e:
                    hashes[f] = None
                    safe_print(f"    哈希计算失败: {f} ({e})")
                done += 1
                if done % 100 == 0 or done == total:
                    safe_print(f"    哈希进度: {done}/{total}")

    # 待确认条目：全部文件哈希一致才算已打包，否则整目录重新打包
    residual_items = []
    for item in confirm_items:
        files = item_files[item]
        if any(hashes.get(f) is None for f in files):
            safe_print(f"    跳过: {item.name} 存在无法读取的文件，本轮不处理")
            continue
        if all(hashes[f] == file_info[f][3] for f in files):
            residual_items.append(item)
        else:
            pack_set.add(item)

    # 生成待打包条目的索引快照；含读取失败文件的条目本轮跳过
    item_snapshots = {}
    for item in pack_set:
        files = item_files[item]
        if files and any(hashes.get(f) is None for f in files):
            safe_print(f"    跳过: {item.name} 存在无法读取的文件，本轮不处理")
            continue
        item_snapshots[item] = [
            (file_info[f][0], file_info[f][1], file_info[f][2], hashes[f])
            for f in files
        ]

    pack_items = [item for item in items if item in item_snapshots]
    return pack_items, item_snapshots, residual_items


def group_items_by_size(items, max_size):
    groups = []
    current_group = []
    current_size = 0

    for item in items:
        item_size = get_item_size(item)
        if item_size > max_size:
            # 超限项单独一个包
            if current_group:
                groups.append(current_group)
                current_group = []
                current_size = 0
            groups.append([item])
            continue

        if current_size + item_size > max_size:
            groups.append(current_group)
            current_group = [item]
            current_size = item_size
        else:
            current_group.append(item)
            current_size += item_size

    if current_group:
        groups.append(current_group)

    return groups


def compress_group(bz_path, output_7z, file_list, password, target_dir):
    target = Path(target_dir)
    rel_files = [str(f.relative_to(target)) for f in file_list]

    cmd = [
        bz_path,
        "c",
        f"-p:{password}",
        "-y",
        str(output_7z.resolve())
    ] + rel_files

    try:
        subprocess.run(
            cmd,
            check=True,
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        return True
    except subprocess.CalledProcessError as e:
        safe_print(f"       错误信息: {e.stderr.strip() if e.stderr else '未知错误'}")
        return False


def test_archive(bz_path, archive_path, password):
    cmd = [
        bz_path,
        "t",
        "-y",
        f"-p:{password}",
        str(archive_path.resolve())
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        return True, "校验通过"
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else (e.stdout.strip() if e.stdout else "密码错误或压缩包损坏")
        return False, f"校验失败: {err_msg}"


def find_bz_exe(bandizip_dir):
    """在安装目录下依次查找 bz.exe、Bandizip.exe"""
    bz_dir = Path(bandizip_dir)
    for name in ("bz.exe", "Bandizip.exe"):
        exe = bz_dir / name
        if exe.exists():
            return str(exe)
    return None


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise _TaskStopped()


def compressor(bz_exe, groups, compress_queue, total_groups, cfg, stats, stop_event):
    """压缩线程：逐组压缩，成功后放入校验队列（哨兵由主线程在所有重试结束后投放）"""
    date_str = datetime.now().strftime("%Y%m%d")
    output_dir = cfg["output_dir"]
    max_retry = cfg["max_retry"]
    password = cfg["password"]

    for idx, group in enumerate(groups, start=1):
        _check_stop(stop_event)
        archive_name = Path(output_dir) / f"{date_str}_{idx:03d}.7z"
        attempt = 0

        while attempt < max_retry:
            _check_stop(stop_event)
            attempt += 1
            safe_print(f"[{idx}/{total_groups}] 正在打包: {archive_name.name} (尝试 {attempt}/{max_retry})...", flush=True)

            if compress_group(bz_exe, archive_name, group, password, cfg["target_dir"]):
                # 压缩成功，交给校验线程
                compress_queue.put((idx, archive_name, group, attempt))
                break
            else:
                # 压缩失败，清理并重试
                if archive_name.exists():
                    archive_name.unlink()

        else:
            # 超过最大重试次数，压缩本身失败
            safe_print(f"    放弃: {archive_name.name} 经 {max_retry} 次尝试仍未压缩成功")
            with _counter_lock:
                stats["fail"] += 1


def verifier(bz_exe, compress_queue, retry_queue, total_groups, index, item_snapshots, cfg, stats, stop_event):
    """校验线程：校验通过则先写索引，再按开关删除源文件，失败则放回重试队列"""
    password = cfg["password"]
    delete_source = cfg["delete_source"]
    max_retry = cfg["max_retry"]

    while True:
        item = compress_queue.get()

        if item is _SENTINEL:
            compress_queue.task_done()
            break

        if stop_event is not None and stop_event.is_set():
            # 用户请求停止：不再校验/删除新包，跳过当前项直接结束
            compress_queue.task_done()
            continue

        idx, archive_name, group, attempt = item

        safe_print(f"    正在校验: {archive_name.name} ...", end="", flush=True)
        test_ok, msg = test_archive(bz_exe, archive_name, password)

        if test_ok:
            safe_print("通过")
            entries = []
            if index is not None:
                # 先写索引再删源文件：索引丢失比文件残留更严重
                entries = [e for f in group for e in item_snapshots.get(f, [])]
                try:
                    if entries:
                        index.upsert_many(entries, archive_name.name)
                    index_ok = True
                except Exception as e:
                    index_ok = False
                    safe_print(f"    索引写入失败: {e}，源文件与压缩包均保留")
            else:
                index_ok = True

            if index_ok:
                if delete_source:
                    for f in group:
                        try:
                            if f.is_dir():
                                shutil.rmtree(f)
                            else:
                                f.unlink()
                        except Exception as e:
                            safe_print(f"    删除源文件失败 {f.name}: {e}")

                index_note = f"已写入索引 {len(entries)} 条，" if index is not None else ""
                source_note = (
                    f"已删除 {len(group)} 个源文件"
                    if delete_source
                    else f"已保留 {len(group)} 个源文件"
                )
                safe_print(f"    {index_note}{source_note}")
                with _counter_lock:
                    stats["success"] += 1
            else:
                with _counter_lock:
                    stats["fail"] += 1
        else:
            safe_print(f"失败 - {msg}")
            # 清理坏包
            if archive_name.exists():
                archive_name.unlink()

            if attempt < max_retry:
                # 放入重试队列，等待压缩线程重新压缩
                retry_queue.put((idx, group, attempt + 1))
            else:
                safe_print(f"    放弃: {archive_name.name} 经 {max_retry} 次尝试仍未通过校验")
                with _counter_lock:
                    stats["fail"] += 1

        compress_queue.task_done()


def run_compress(cfg, log_cb=_default_log_cb, stop_event=None):
    """
    压缩主流程（可复用入口，供 GUI/前端调用）。

    cfg 字段：
        target_dir        待压缩的源目录
        bandizip_dir      Bandizip 安装目录
        password          压缩包密码（可为空串，表示不加密）
        max_package_size  单个包体积上限（字节）
        max_retry          压缩/校验失败最大重试次数
        verify_workers     校验线程数
        hash_workers       哈希计算线程数
        enable_hash_index  是否启用哈希判重与索引入库
        delete_source      压缩校验通过后是否删除源文件

    返回: {"success", "fail", "total_groups", "output_dir", "index_db", "index_count"}
    """
    global _LOG_CB
    _LOG_CB = log_cb
    log = _LOG_CB

    target_dir = os.path.normpath(cfg["target_dir"])
    bandizip_dir = cfg.get("bandizip_dir") or DEFAULT_BANDIZIP_DIR
    password = cfg.get("password") or ""
    max_package_size = cfg.get("max_package_size") or DEFAULT_MAX_PACKAGE_SIZE
    max_retry = cfg.get("max_retry") or DEFAULT_MAX_RETRY
    verify_workers = cfg.get("verify_workers") or DEFAULT_VERIFY_WORKERS
    hash_workers = cfg.get("hash_workers") or DEFAULT_HASH_WORKERS
    enable_hash_index = cfg.get("enable_hash_index", True)
    delete_source = cfg.get("delete_source", True)

    output_dir = os.path.join(target_dir, "compressed_output")
    index_db = build_index_db_path(target_dir)
    cfg_full = {
        "target_dir": target_dir,
        "bandizip_dir": bandizip_dir,
        "password": password,
        "max_package_size": max_package_size,
        "max_retry": max_retry,
        "verify_workers": verify_workers,
        "hash_workers": hash_workers,
        "enable_hash_index": enable_hash_index,
        "delete_source": delete_source,
        "output_dir": output_dir,
    }
    stats = {"success": 0, "fail": 0}

    result = {
        "success": 0,
        "fail": 0,
        "total_groups": 0,
        "output_dir": output_dir,
        "index_db": index_db if enable_hash_index else None,
        "index_count": None,
    }

    log(f"目标目录: {target_dir}")
    log(f"哈希判重与索引入库: {'开启' if enable_hash_index else '关闭'}")
    log(f"压缩后删除源文件: {'是' if delete_source else '否（保留源文件）'}")

    target_path = Path(target_dir)
    output_path = Path(output_dir)

    if not target_path.exists():
        log(f"错误: 目标路径不存在 {target_dir}")
        result["fail"] = 1
        return result

    bz_exe = find_bz_exe(bandizip_dir)
    if not bz_exe:
        log(f"错误: 未找到 Bandizip，请确认安装目录: {bandizip_dir}")
        result["fail"] = 1
        return result

    output_path.mkdir(parents=True, exist_ok=True)

    items = get_items_sorted_by_ctime(target_path, output_dir)
    if not items:
        log("目标目录下没有可压缩的文件或目录。")
        return result

    log(f"正在扫描并按创建时间排序，共找到 {len(items)} 个项目...")

    index = DedupIndex(index_db) if enable_hash_index else None
    try:
        if index is not None:
            index_snapshot = index.load_all()
            log(f"判重索引已有 {len(index_snapshot)} 条记录")

            pack_items, item_snapshots, residual_items = dedup_filter(
                items, index_snapshot, target_path, hash_workers
            )
        else:
            log("哈希判重与索引入库已关闭，跳过判重，全部项目参与打包")
            pack_items, item_snapshots, residual_items = items, {}, []

        if residual_items:
            if delete_source:
                # 已确认打包的残留条目直接清理（索引中只记录校验通过的包，删除是安全的）
                for item in residual_items:
                    try:
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                        safe_print(f"已打包过，清理残留: {item.name}")
                    except Exception as e:
                        safe_print(f"清理残留失败 {item.name}: {e}")
            else:
                safe_print(f"已打包过但保留源文件的残留 {len(residual_items)} 项，按设置跳过清理")

        log(f"\n判重结果: 跳过 {len(residual_items)} 个已打包项目，待打包 {len(pack_items)} 个项目")

        if not pack_items:
            log("没有需要打包的内容，结束。")
            return result

        groups = group_items_by_size(pack_items, max_package_size)
        size_gb = max_package_size / (1024 * 1024 * 1024)
        result["total_groups"] = len(groups)
        log(f"已按 <{size_gb}GB 条件切分为 {len(groups)} 个独立包组，开始打包...\n")

        # 压缩→校验 队列
        compress_queue = Queue()
        # 校验失败→重试压缩 队列
        retry_queue = Queue()

        # 启动校验线程
        verify_threads = []
        for _ in range(verify_workers):
            t = threading.Thread(
                target=verifier,
                args=(bz_exe, compress_queue, retry_queue, len(groups), index,
                      item_snapshots, cfg_full, stats, stop_event),
                daemon=True,
            )
            t.start()
            verify_threads.append(t)

        try:
            # 先处理初次分组
            compressor(bz_exe, groups, compress_queue, len(groups), cfg_full, stats, stop_event)

            # 处理校验失败的重试：每轮等校验队列清空后再检查重试队列，直到没有新重试产生
            date_str = datetime.now().strftime("%Y%m%d")
            while True:
                compress_queue.join()  # 等待已入队的包全部校验完毕
                if retry_queue.empty():
                    break

                retry_groups = []
                while not retry_queue.empty():
                    retry_groups.append(retry_queue.get())

                for idx, group, next_attempt in retry_groups:
                    _check_stop(stop_event)
                    archive_name = output_path / f"{date_str}_{idx:03d}.7z"
                    attempt = next_attempt - 1  # 已尝试的次数

                    while attempt < max_retry:
                        _check_stop(stop_event)
                        attempt += 1
                        safe_print(f"[{idx}/{len(groups)}] 正在重试打包: {archive_name.name} (尝试 {attempt}/{max_retry})...", flush=True)

                        if compress_group(bz_exe, archive_name, group, password, target_dir):
                            compress_queue.put((idx, archive_name, group, attempt))
                            break
                        else:
                            if archive_name.exists():
                                archive_name.unlink()
                    else:
                        safe_print(f"    放弃: {archive_name.name} 经 {max_retry} 次尝试仍未压缩成功")
                        with _counter_lock:
                            stats["fail"] += 1
        finally:
            # 全部压缩与校验完成（或用户停止），通知校验线程退出
            for _ in range(verify_workers):
                compress_queue.put(_SENTINEL)
            for t in verify_threads:
                t.join()

        result["success"] = stats["success"]
        result["fail"] = stats["fail"]
        if index is not None:
            result["index_count"] = index.count()

        log("\n" + "=" * 60)
        if stop_event is not None and stop_event.is_set():
            log("任务已停止（当前包完成后的剩余包未处理）")
        log(f"处理完成！共 {result['total_groups']} 个包组:")
        index_note = "已写入索引，" if index is not None else ""
        source_note = "源文件已删除" if delete_source else "源文件已保留"
        log(f"  - 成功: {stats['success']} 个（{index_note}{source_note}）")
        log(f"  - 失败: {stats['fail']} 个（源文件保留）")
        if index is not None:
            log(f"索引库: {index_db}（{result['index_count']} 条记录）")
        log(f"输出目录: {output_dir}")
    finally:
        if index is not None:
            index.close()

    return result


def main():
    args = parse_args()
    cfg = {
        "target_dir": args.target_dir,
        "bandizip_dir": DEFAULT_BANDIZIP_DIR,
        "password": "",  # 默认不加密；CLI 如需密码请在此填写或在前端页面设置
        "max_package_size": DEFAULT_MAX_PACKAGE_SIZE,
        "max_retry": DEFAULT_MAX_RETRY,
        "verify_workers": args.verify_workers if args.verify_workers else DEFAULT_VERIFY_WORKERS,
        "hash_workers": args.hash_workers if args.hash_workers else DEFAULT_HASH_WORKERS,
        "enable_hash_index": args.hash_index,
        "delete_source": args.delete_source,
    }
    run_compress(cfg, log_cb=_default_log_cb)


if __name__ == "__main__":
    main()
