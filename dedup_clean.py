"""断传检查与哈希去重工具（独立于 batch_compress.py）

用于清理归档目录中的断传残留与内容重复文件：
  一、同目录内内容哈希相同的文件视为重复（含不同名），保留一个，其余副本
      直接删除（进 Windows 回收站）。（0 字节文件也按哈希归组，仅保留一个）
  二、孤立的 0 字节文件（断传失败残留）直接删除（进 Windows 回收站）。

删除一律放入 Windows 回收站（可恢复）。不做 "A.mp4 / A (2).mp4" 成对判定，
也不做连号序列保护，(N) 后缀文件与普通文件同等对待。

用法:
    python dedup_clean.py [-h] [--target-dir 目录] [--preview]
"""
import argparse
import hashlib
import os
import re
import sys
import threading
from pathlib import Path

DEFAULT_TARGET_DIR = r"F:\ll"  # 目标目录默认值，可用命令行参数覆盖
# 断传补丁文件后缀，如 "3395 (2).mp4" -> ("3395.mp4", 2)；不匹配则返回 None
_CP_PATTERN = re.compile(r"^(?P<base>.+) \((?P<num>\d+)\)(?P<ext>\.\w+)$")

_print_lock = threading.Lock()


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
    """解析命令行参数：目标目录 + 是否仅预览"""
    parser = argparse.ArgumentParser(
        description="断传检查与哈希去重：清理归档目录中断传残留与内容重复文件",
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


class _TaskStopped(Exception):
    pass


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise _TaskStopped()


def dedup_clean(target_dir, preview=False, log_cb=None, stop_event=None):
    """断传检查与哈希去重主流程。

    返回 deleted_count。preview=True 时只统计不改动文件。
    log_cb: 日志回调 text, end；默认走模块级 _LOG_CB。
    stop_event: 设置后在循环边界抛出 _TaskStopped 终止任务。
    """
    global _LOG_CB
    if log_cb is not None:
        _LOG_CB = log_cb

    target = Path(target_dir)
    out_root = target / "compressed_output"

    if not target.exists():
        print(f"错误: 目标路径不存在 {target_dir}")
        return 0

    all_files = []
    for fd in target.rglob("*"):
        try:
            if not fd.is_file():
                continue
            if fd.is_relative_to(out_root):
                continue  # 压缩输出目录不参与
        except OSError:
            continue
        all_files.append(fd)

    def stat_size(p):
        try:
            return p.stat().st_size
        except OSError:
            return None

    def content_hash(p):
        try:
            return compute_hash(p)
        except OSError:
            return None

    deleted = 0

    # 一、按 (父目录, 内容哈希) 全量分组的精确去重。
    #     [优化] 先按 (父目录, 文件大小) 预分组：同目录中内容相同的文件大小必然相同，
    #     大小不同的文件无需计算哈希。仅对组内 >=2 个文件的分组计算哈希细分，
    #     绝大多数文件只做一次 stat()，避免全量读盘（重复拷贝通常大小一致）。
    base_names = {}
    size_groups = {}
    for fd in all_files:
        cp = parse_cp_stem(fd.name)
        base_name = cp[0] if cp else fd.name
        base_names[str(fd)] = base_name
        sz = stat_size(fd)
        if sz is None:
            continue
        size_groups.setdefault((str(fd.parent), sz), []).append(fd)

    hash_groups = {}
    for (parent_s, sz), files in size_groups.items():
        _check_stop(stop_event)
        if len(files) < 2:
            continue  # 组内仅一个文件，无重复可能
        for fd in files:
            h = content_hash(fd)
            if h is None:
                continue
            hash_groups.setdefault((parent_s, h), []).append(fd)

    protected = set()  # 已被去重删除的文件，不再进入空文件流程
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
            protected.add(str(p))
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

    # 二、孤立的 0 字节文件：清理无内容价值的空文件（断传失败残留）。
    #     (N) 后缀文件与普通文件同等对待，0 字节一律删除进回收站。
    for fd in all_files:
        _check_stop(stop_event)
        if str(fd) in protected:
            continue
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

    return deleted


def main(argv=None):
    args = parse_args(argv)
    target_dir = os.path.normpath(args.target_dir)
    mode = "预览（不修改文件）" if args.preview else "执行"
    print(f"目标目录: {target_dir}")
    print(f"模式: {mode}")
    deleted = dedup_clean(target_dir, preview=args.preview)
    print(f"\n完成: 删除到回收站 {deleted} 个"
          f"{'（预览未实际修改）' if args.preview else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
