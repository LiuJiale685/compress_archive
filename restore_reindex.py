"""恢复旧压缩包并补全 hash 入库的一体化流程脚本

场景：存有旧的 .7z 压缩包，希望解压回文件、补齐 BLAKE2b-256 hash，
并将结果登记进判重索引库，避免后续重复计算（不重新压缩）。

流程（全部复用现有模块）：
  1. 解压 source_dir 下全部 .7z 到 target_dir（batch_decompress.run_decompress）
  2. 清理补 hash：对 target_dir 全部文件计算 hash 并落盘临时缓存
     （默认仅计算不删除 = 全量 hash 快照；加 --delete-duplicates 才真正去重/删空文件进回收站）
  3. 入库：读取 hash 缓存，连同 size/mtime 写入 <target 末层>_dedup_index.db，
     archive 标记为 "UNPACKED"（表明记录来自解压登记，而非压缩归档动作），
     入库后删除临时 hash 缓存。

用法:
    python restore_reindex.py --source-dir <压缩包目录> --target-dir <目标目录> \\
        [--bandizip-dir 默认 C:\\Program Files\\Bandizip] [--password 压缩包密码] \\
        [--delete-duplicates] [--hash-workers N] [--max-retry N] [--keep-7z]

注意：
  - 入库记录 archite 标记为 UNPACKED，仅用于 hash 参考；
    若后续对同一 target_dir 原地重新压缩，判重命中会将其视为已归档并可能删除源文件，
    请确认旧 .7z 确实保留后再使用。
"""
import argparse
import os
import sys
from pathlib import Path

import batch_compress
import batch_decompress
import dedup_clean
import hash_cache

# 入库 archive 标记：表明记录来自解压登记，而非压缩归档
UNPACKED_TAG = "UNPACKED"


def index_restored(target_dir, cache_file, index_db, log):
    """把 hash 缓存登记进判重索引库；返回成功入库条数"""
    index = batch_compress.DedupIndex(index_db)
    try:
        cache = hash_cache.load_cache(cache_file)
        if not cache:
            log(f"没有可入库的 hash 缓存: {cache_file}")
            return 0
        entries = []
        missing = []
        for rel, info in cache.items():
            p = Path(target_dir) / rel
            try:
                st = p.stat()
            except OSError:
                missing.append(rel)
                continue
            entries.append((rel, st.st_size, st.st_mtime_ns, info["h"]))
        if entries:
            index.upsert_many(entries, UNPACKED_TAG)
        if missing:
            shown = ", ".join(missing[:5])
            more = f" 等 {len(missing)} 项" if len(missing) > 5 else ""
            log(f"跳过 {len(missing)} 个已不存在的文件（未入库）: {shown}{more}")
        return len(entries)
    finally:
        index.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="恢复旧压缩包：解压 + 清理补 hash + 登记入库（不重新压缩）",
    )
    parser.add_argument(
        "--source-dir",
        required=True,
        help="旧压缩包目录（含 .7z）",
    )
    parser.add_argument(
        "--target-dir",
        required=True,
        help="目标目录（解压输出 + 清理补 hash + 入库对象）",
    )
    parser.add_argument("--bandizip-dir", default=batch_decompress.DEFAULT_BANDIZIP_DIR,
                        help=f"Bandizip 安装目录（默认: {batch_decompress.DEFAULT_BANDIZIP_DIR}）")
    parser.add_argument("--password", default="", help="压缩包密码（留空表示不加密）")
    parser.add_argument("--delete-duplicates", action="store_true",
                        help="清理阶段真正执行：删除 0 字节与同目录同 hash 的重复副本（进回收站）；"
                             "默认仅计算 hash 不删除（全量快照）")
    parser.add_argument("--keep-7z", action="store_true",
                        help="解压成功后保留 .7z 包（默认删除）")
    parser.add_argument("--hash-workers", type=int, default=None,
                        help=f"哈希计算线程数（默认按 CPU 核数自适应: {hash_cache.default_hash_workers()}）")
    parser.add_argument("--max-retry", type=int, default=batch_decompress.DEFAULT_MAX_RETRY,
                        help=f"解压失败最大重试次数（默认: {batch_decompress.DEFAULT_MAX_RETRY}）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    source_dir = os.path.normpath(args.source_dir)
    target_dir = os.path.normpath(args.target_dir)

    print("=" * 60)
    print("恢复流程: 解压 -> 清理补 hash -> 登记入库")
    print(f"压缩包目录: {source_dir}")
    print(f"目标目录:   {target_dir}")
    print(f"清理模式:   {'执行删除(去重+空文件)' if args.delete_duplicates else '仅计算 hash(不删除)'}")
    print("=" * 60)

    # ---- 1. 解压 ----
    decompress_cfg = {
        "source_dir": source_dir,
        "output_dir": target_dir,
        "bandizip_dir": args.bandizip_dir,
        "password": args.password,
        "max_retry": args.max_retry,
        "delete_after_extract": not args.keep_7z,
    }
    r1 = batch_decompress.run_decompress(decompress_cfg, log_cb=print)
    if r1["fail"] > 0:
        print(f"警告: 有 {r1['fail']} 个压缩包解压失败，继续后续步骤（失败包的文件缺 hash，未入库）")

    # ---- 2. 清理补 hash（默认预览=全量快照） ----
    deleted = dedup_clean.dedup_clean(
        target_dir,
        preview=not args.delete_duplicates,
        log_cb=print,
        hash_workers=args.hash_workers,
    )

    # ---- 3. 登记入库 ----
    cache_file = hash_cache.cache_path(target_dir)
    index_db = batch_compress.build_index_db_path(target_dir)
    n = index_restored(target_dir, cache_file, index_db, print)
    print(f"已登记 {n} 个文件到索引库: {index_db}（archive={UNPACKED_TAG}）")

    # 入库完成，清理临时 hash 缓存（保留则后续压缩亦会复用，但入库已持久化，不再需要）
    try:
        if os.path.exists(cache_file):
            os.remove(cache_file)
            print(f"已清理临时 hash 缓存: {cache_file}")
    except OSError as e:
        print(f"清理 hash 缓存失败: {e}")

    print("=" * 60)
    print(f"完成。解压成功 {r1['success']}/{r1['total']} 个包，"
          f"清理{'删除' if args.delete_duplicates else '标记'} {deleted} 个文件，"
          f"登记入库 {n} 条 hash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
