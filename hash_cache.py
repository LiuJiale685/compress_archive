"""内容 hash 能力复用模块：清理与压缩共享同一实现与临时结果

约定：
- 哈希算法统一 BLAKE2b-256（1MB 分块读取），清理/压缩结果完全一致
- 清理阶段对全部文件取 hash、去重后把"保留文件"的 hash 落盘为临时 JSON 缓存（预览模式保留全量快照）
- 压缩阶段直接读取该缓存复用 hash，缓存缺失的文件现场补算
- JSON 结构：{rel_posix_path: {"h": hex, "s": size}}，s 为字节数，仅作参考，不参与比对
"""
import hashlib
import json
import os

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HASH_CACHE_SUFFIX = ".hash_cache.json"


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


def default_hash_workers():
    """哈希并行线程数：直接用 CPU 核数；探测失败退化 4（None or 4）"""
    return os.cpu_count() or 4


def cache_path(target_dir):
    """临时 hash 缓存路径 = 目标目录 + 末层名 + .hash_cache.json（位于目录内，扫描需排除）"""
    base = os.path.basename(os.path.normpath(str(target_dir)))
    return os.path.join(str(target_dir), f"{base}{HASH_CACHE_SUFFIX}")


def write_cache(cache_file, hash_map):
    """原子写缓存：先写 .tmp 再 os.replace，避免进程中断留下半截 JSON"""
    tmp = str(cache_file) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hash_map, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, str(cache_file))


def load_cache(cache_file):
    """读取缓存；不存在或损坏返回 {}"""
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("h"), str):
            out[k] = {"h": v["h"], "s": v.get("s")}
    return out


def _calc_with_size(p, rel):
    return rel, {"h": compute_hash(p), "s": p.stat().st_size}


def compute_missing(rel_map, missing_rels, log_cb=None, stop_event=None, workers=None):
    """现场补算缓存缺失文件的 hash，返回 {rel: {"h", "s"}}"""
    targets = [(rel, rel_map[rel]) for rel in missing_rels if rel in rel_map]
    if not targets:
        return {}
    workers = workers or default_hash_workers()
    fills = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_calc_with_size, p, rel) for rel, p in targets]
        for fut in as_completed(futs):
            stop_event and _check_stop(stop_event)
            try:
                rel, info = fut.result()
                fills[rel] = info
            except Exception:
                pass
            done += 1
            if log_cb and (done % 100 == 0 or done == len(targets)):
                log_cb(f"    补算哈希进度: {done}/{len(targets)}")
    return fills


class _TaskStopped(Exception):
    pass


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise _TaskStopped()
