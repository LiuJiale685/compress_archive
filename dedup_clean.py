"""断传检查与哈希去重工具

用于清理归档目录中的断传残留与内容重复文件，并为后续压缩提供复用的 hash 结果：
  一、孤立的 0 字节文件（断传失败残留）直接删除（进 Windows 回收站）。
  二、对全部剩余文件计算 BLAKE2b-256 哈希（并行），同目录内哈希相同视为重复，
      保留一个，其余副本删除（进 Windows 回收站）。
  三、将保留文件的哈希落盘为临时缓存 <目标目录末层>.hash_cache.json，
      供 batch_compress.py 压缩判重阶段直接复用（能力复用，不再重复计算）。

删除一律放入 Windows 回收站（可恢复）。不做 "A.mp4 / A (2).mp4" 成对判定，
也不做连号序列保护，(N) 后缀文件与普通文件同等对待。

用法:
    python dedup_clean.py [-h] [--target-dir 目录] [--preview]
"""
import argparse
import os
import re
import sys
import threading
from pathlib import Path

import hash_cache

DEFAULT_TARGET_DIR = r"F:\ll"  # 目标目录默认值，可用命令行参数覆盖
# 断传补丁文件后缀，如 "3395 (2).mp4" -> ("3395.mp4", 2)；不匹配则返回 None
_CP_PATTERN = re.compile(r"^(?P<base>.+) \((?P<num>\d+)\)(?P<ext>\.\w+)$")

_print_lock = threading.Lock()


def default_hash_workers():
    """哈希并行线程数默认值：直接取 CPU 核数；探测失败退化 4（与 hash_cache 一致）"""
    return os.cpu_count() or 4


# ---- 删除到 Windows 回收站（SHFileOperationW + FOF_ALLOWUNDO） ----
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _FO_DELETE = 0x0003
    _FOF_ALLOWUNDO = 0x0040
    _FOF_NOCONFIRMATION = 0x0010
    _FOF_SILENT = 0x0004
    _FOF_NOERRORUI = 0x0400

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    _SHFileOperationW = ctypes.WinDLL("shell32", use_last_error=True).SHFileOperationW
    _SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    _SHFileOperationW.restype = wintypes.BOOL

    def delete_file_to_trash(path):
        """把单个文件删除到 Windows 回收站；成功返回 True。

        网络共享（UNC）路径无回收站概念，直接返回 False 而不执行删除，
        避免在无法进回收站的情况下造成永久删除。
        """
        drive = os.path.splitdrive(os.path.abspath(str(path)))[0]
        if drive.startswith("\\\\"):  # UNC 网络共享路径
            return False
        op = _SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=_FO_DELETE,
            pFrom=str(path) + "\0\0",
            pTo=None,
            fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI,
            fAnyOperationsAborted=False,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        res = _SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
else:

    def delete_file_to_trash(path):
        # 非 Windows 平台无回收站，删除操作不可用
        return False


def _default_log_cb(text, end="\n"):
    with _print_lock:
        print(text, end=end)


# 当前日志回调，由 dedup_clean 设置；默认为 print，保证 CLI 兼容
_LOG_CB = _default_log_cb


def safe_print(*args, **kwargs):
    """统一经日志回调输出（text 拼接 + end 透传）"""
    end = kwargs.pop("end", "\n")
    text = " ".join(str(a) for a in args)
    _LOG_CB(text, end=end)


def parse_args(argv=None):
    """解析命令行参数：目标目录 / 是否仅预览"""
    parser = argparse.ArgumentParser(
        description="断传检查与哈希去重：清理归档目录中断传残留与内容重复文件，并产出供压缩复用的 hash 缓存",
    )
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=DEFAULT_TARGET_DIR,
        help=f"待清理的目标目录（默认: {DEFAULT_TARGET_DIR}）",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="仅预览将执行的操作，不实际删除任何文件",
    )
    parser.add_argument(
        "--hash-workers",
        type=int,
        default=None,
        help=f"哈希计算线程数（默认按 CPU 核数自适应: {default_hash_workers()}）",
    )
    return parser.parse_args(argv)


def parse_cp_stem(name):
    """
    解析断传补丁文件的文件名。
    返回 (基础文件名, 序号)；非补丁文件返回 None。
    仅允许 Windows 复制产生的 '(数字)' 格式，避免误伤 '(1)' 这样的正常文件名。
    """
    m = _CP_PATTERN.match(name)
    if m and m.group("num") == "1":
        return None  # 排除 "(1)"：无法确定与哪个基础文件名配对
    if m:
        return m.group("base") + m.group("ext"), int(m.group("num"))
    return None


class _TaskStopped(Exception):
    pass


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise _TaskStopped()


def dedup_clean(target_dir, preview=False, log_cb=None, stop_event=None, hash_workers=None):
    """断传检查与哈希去重主流程（能力复用：产出供压缩判重复用的 hash 缓存）。

    步骤：
      1. 先删除孤立的 0 字节文件（断传失败残留）
      2. 对全部剩余文件并行计算 BLAKE2b-256 哈希（与 batch_compress 共用实现，线程数可配）
      3. 同目录内哈希相同的文件视为重复，保留一个（优先保留不带 (N) 后缀的），
         其余副本删除进回收站
      4. 将保留文件的哈希写盘为 <target_dir 末层>.hash_cache.json，
         供压缩阶段直接读取复用

    返回 deleted_count。preview=True 时只统计、不改动文件，但仍产出 hash 缓存。
    log_cb: 日志回调 text, end；默认走模块级 _LOG_CB。
    stop_event: 设置后在循环边界抛出 _TaskStopped 终止任务。
    hash_workers: 哈希计算线程数；None 时按 CPU 核数自适应。
    """
    global _LOG_CB
    if log_cb is not None:
        _LOG_CB = log_cb

    target = Path(target_dir)
    if not target.exists():
        safe_print(f"错误: 目标路径不存在 {target_dir}")
        return 0

    deleted = 0

    # 一、收集全部文件（排除 compressed_output 与 hash 缓存自身）
    all_files = []
    for fd in target.rglob("*"):
        try:
            if not fd.is_file():
                continue
            if fd.is_relative_to(target / "compressed_output"):
                continue  # 压缩输出目录不参与
            if fd.name.endswith(hash_cache.HASH_CACHE_SUFFIX):
                continue  # 不清理/不扫描缓存自身
        except OSError:
            continue
        all_files.append(fd)

    def stat_size(p):
        try:
            return p.stat().st_size
        except OSError:
            return None

    # 二、孤立的 0 字节文件：清理无内容价值的空文件（断传失败残留）。
    #     (N) 后缀文件与普通文件同等对待，0 字节一律删除进回收站。
    for fd in all_files:
        _check_stop(stop_event)
        if stat_size(fd) == 0:
            if preview:
                safe_print(f"    [预览] 将删除(回收站): 孤立空文件 {fd.relative_to(target)}")
            else:
                try:
                    if delete_file_to_trash(fd):
                        safe_print(f"    删除到回收站: 孤立空文件 {fd.relative_to(target)}")
                        deleted += 1
                    else:
                        safe_print(f"    删除失败（无回收站支持或操作取消）: {fd.relative_to(target)}")
                except OSError as e:
                    safe_print(f"    删除失败: {fd.relative_to(target)} ({e})")

    # 三、对全部剩余文件并行计算哈希（与压缩共用 hash_cache.compute_hash）
    remaining = [fd for fd in all_files if stat_size(fd) != 0]
    rel_map = {}    # rel_posix -> Path（relative_to(target)）
    for fd in remaining:
        rel_map[fd.relative_to(target).as_posix()] = fd

    total = len(rel_map)
    hash_map = {}   # rel_posix -> {"h": hex, "s": size}
    if total:
        safe_print(f"正在计算哈希进行判重，共 {total} 个文件...")
        # 并行计算，进度逐批上报（与压缩共用 hash_cache 的算法核心）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        workers = max(1, hash_workers or hash_cache.default_hash_workers())
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(hash_cache.compute_hash, p): rel for rel, p in rel_map.items()}
            for fut in as_completed(futs):
                _check_stop(stop_event)
                rel = futs[fut]
                try:
                    hash_map[rel] = {"h": fut.result(), "s": rel_map[rel].stat().st_size}
                except Exception:
                    pass  # 读取失败的文件不参与去重与缓存
                done += 1
                if done % 100 == 0 or done == total:
                    safe_print(f"    哈希进度: {done}/{total}")

    # 四、按 (父目录, 哈希) 归组去重：同目录内容相同视为重复
    base_names = {}
    for fd in remaining:
        cp = parse_cp_stem(fd.name)
        base_names[str(fd)] = cp[0] if cp else fd.name
    hash_groups = {}
    for rel, info in hash_map.items():
        parent = str(rel_map[rel].parent)
        hash_groups.setdefault((parent, info["h"]), []).append(rel_map[rel])

    for (parent_s, h), files in hash_groups.items():
        _check_stop(stop_event)
        if len(files) < 2:
            continue
        # 保留一个：优先保留"基础名"文件（不带 (N) 后缀），否则保留文件名最小的
        def base_rank(p):
            return (p.name != base_names.get(str(p), p.name))  # True 表示带后缀

        keep = min(files, key=lambda p: (base_rank(p), p.name))
        for p in files:
            if p is keep:
                continue
            if preview:
                safe_print(f"    [预览] 将删除(回收站): 重复副本 {p.relative_to(target)}（与 {keep.name} 内容相同）")
            else:
                try:
                    if delete_file_to_trash(p):
                        safe_print(f"    删除到回收站: 重复副本 {p.relative_to(target)}（与 {keep.name} 内容相同）")
                        deleted += 1
                    else:
                        safe_print(f"    删除失败（无回收站支持或操作取消）: {p.relative_to(target)}")
                except OSError as e:
                    safe_print(f"    删除失败: {p.relative_to(target)} ({e})")
            # 执行模式：被删文件的哈希不再进入复用缓存。
            # 预览模式：不删除文件，缓存保留全量 hash（能力复用 = 补全快照，供压缩直接复用）
            if not preview:
                rel_delete = p.relative_to(target).as_posix()
                hash_map.pop(rel_delete, None)

    # 五、将保留文件哈希写盘，供压缩判重阶段复用（能力复用）
    cache_file = hash_cache.cache_path(target_dir)
    hash_cache.write_cache(cache_file, hash_map)
    if preview:
        safe_print(f"已保存 {len(hash_map)} 个文件的全量 hash 快照（预览，未删除文件）: {cache_file}")
    else:
        safe_print(f"已保存 {len(hash_map)} 个文件的哈希缓存: {cache_file}")

    return deleted


def main(argv=None):
    args = parse_args(argv)
    target_dir = os.path.normpath(args.target_dir)
    mode = "预览（不修改文件）" if args.preview else "执行"
    print(f"目标目录: {target_dir}")
    print(f"模式: {mode}")
    if args.hash_workers:
        print(f"哈希计算线程数: {args.hash_workers}")
    deleted = dedup_clean(
        target_dir,
        preview=args.preview,
        hash_workers=args.hash_workers if args.hash_workers else None,
    )
    print(f"\n完成: 删除到回收站 {deleted} 个"
          f"{'（预览未实际修改）' if args.preview else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
