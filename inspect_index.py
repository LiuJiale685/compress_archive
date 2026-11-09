"""
查看 _dedup_index.db 判重索引（只读，不会修改数据库）

用法示例：
    python inspect_index.py                       # 概览
    python inspect_index.py --archives            # 按压缩包统计
    python inspect_index.py -l --limit 50        # 列出记录
    python inspect_index.py -l --filter-path 电影 # 按路径关键字过滤
    python inspect_index.py -l --filter-archive 20261017_003.7z
    python inspect_index.py -l --sort size       # 按文件大小排序
    python inspect_index.py --duplicates          # 内容相同的不同路径
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dedup_index.db")

SORTS = {
    "path": "rel_path ASC",
    "size": "size DESC",
    "time": "packed_at DESC, rel_path ASC",
    "archive": "archive ASC, rel_path ASC",
}


def human_size(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.0f} {u}" if u == "B" else f"{v:.2f} {u}"
        v /= 1024
    return f"{v:.2f} TB"


def open_db(path, mode="ro"):
    """打开索引库；不存在时返回 None（mode: ro 只读 / rw 读写）"""
    if not os.path.exists(path):
        return None
    if mode == "ro":
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return sqlite3.connect(path)


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_where(args):
    clauses, params = [], []
    if args.filter_archive:
        clauses.append("archive LIKE ? ESCAPE '\\'")
        params.append(f"%{like_escape(args.filter_archive)}%")
    if args.filter_path:
        clauses.append("rel_path LIKE ? ESCAPE '\\'")
        params.append(f"%{like_escape(args.filter_path)}%")
    if args.filter_hash:
        clauses.append("hash LIKE ?")
        params.append(f"{args.filter_hash.lower()}%")
    if args.min_size:
        clauses.append("size >= ?")
        params.append(args.min_size)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


# ---------------- 结构化查询函数（供 GUI/前端复用） ----------------

def get_summary(conn):
    """概览：返回 dict 结构数据"""
    total, size, archives = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0), COUNT(DISTINCT archive) FROM packed_files"
    ).fetchone()

    first = last = avg = None
    if total:
        first, last = conn.execute(
            "SELECT MIN(packed_at), MAX(packed_at) FROM packed_files"
        ).fetchone()
        avg = size / total

    top_files = [
        {"rel_path": rel, "size": s, "archive": archive}
        for rel, s, archive in conn.execute(
            "SELECT rel_path, size, archive FROM packed_files ORDER BY size DESC LIMIT 5"
        )
    ]
    recent_archives = [
        {"archive": archive, "cnt": cnt, "size": s, "ts": ts}
        for archive, cnt, s, ts in conn.execute(
            "SELECT archive, COUNT(*), SUM(size), MAX(packed_at) FROM packed_files"
            " GROUP BY archive ORDER BY MAX(packed_at) DESC LIMIT 5"
        )
    ]
    return {
        "total": total, "size": size, "archives": archives,
        "first": first, "last": last, "avg": avg,
        "top_files": top_files, "recent_archives": recent_archives,
    }


def get_archives(conn):
    """按压缩包统计：返回 {rows, totals}"""
    rows = [
        {"archive": archive, "cnt": cnt, "size": s, "ts": ts}
        for archive, cnt, s, ts in conn.execute(
            "SELECT archive, COUNT(*), SUM(size), MIN(packed_at) FROM packed_files"
            " GROUP BY archive ORDER BY archive ASC"
        )
    ]
    totals = {
        "cnt": sum(r["cnt"] for r in rows),
        "size": sum(r["size"] for r in rows if r["size"] is not None),
    }
    return {"rows": rows, "totals": totals}


def query_records(conn, *, filter_archive=None, filter_path=None, filter_hash=None,
                  min_size=None, sort="path", limit=100, offset=0):
    """记录列表查询：返回 {matched, limit, offset, sort, rows}"""
    args = argparse.Namespace(
        filter_archive=filter_archive, filter_path=filter_path,
        filter_hash=filter_hash, min_size=min_size,
    )
    where, params = build_where(args)
    order = SORTS.get(sort, SORTS["path"])
    lim = "" if limit <= 0 else f" LIMIT {limit} OFFSET {offset}"

    matched = conn.execute(f"SELECT COUNT(*) FROM packed_files{where}", params).fetchone()[0]
    rows = [
        {"archive": archive, "size": s, "packed_at": ts, "hash": h, "rel_path": rel}
        for archive, s, ts, h, rel in conn.execute(
            f"SELECT archive, size, packed_at, hash, rel_path FROM packed_files{where}"
            f" ORDER BY {order}{lim}",
            params,
        )
    ]
    return {"matched": matched, "limit": limit, "offset": offset, "sort": sort, "rows": rows}


def get_duplicates(conn):
    """内容重复（相同哈希不同路径）：返回 {groups}"""
    dup_hashes = conn.execute(
        "SELECT hash FROM packed_files GROUP BY hash HAVING COUNT(*) > 1"
        " ORDER BY COUNT(*) DESC"
    ).fetchall()
    groups = []
    for (h,) in dup_hashes:
        rows = conn.execute(
            "SELECT rel_path, size, archive FROM packed_files WHERE hash = ? ORDER BY rel_path",
            (h,),
        ).fetchall()
        groups.append({
            "hash": h, "size": rows[0][1], "count": len(rows),
            "rows": [{"rel_path": rel, "archive": archive} for rel, _s, archive in rows],
        })
    return {"groups": groups}


# ---------------- 原 CLI 输出函数（复用结构化数据，输出保持原样） ----------------

def print_summary(conn, db_path):
    data = get_summary(conn)
    print(f"索引库: {db_path}")
    print(f"文件记录: {data['total']} 条")
    print(f"原始总大小: {human_size(data['size'])}")
    print(f"涉及压缩包: {data['archives']} 个")

    if data["total"] == 0:
        return

    print(f"打包时间范围: {data['first']} ~ {data['last']}")
    print(f"平均文件大小: {human_size(data['avg'])}")

    print("\n最大的 5 个文件:")
    for f in data["top_files"]:
        print(f"  {human_size(f['size']):>10}  {f['rel_path']}   <- {f['archive']}")

    print("\n最近的 5 个压缩包:")
    for a in data["recent_archives"]:
        print(f"  {a['archive']}  {a['cnt']:>6} 个文件  {human_size(a['size']):>10}  {a['ts']}")


def print_archives(conn):
    data = get_archives(conn)
    if not data["rows"]:
        print("索引为空。")
        return
    print(f"{'压缩包':<24}{'文件数':>8}{'原始大小':>14}  打包时间")
    print("-" * 70)
    for r in data["rows"]:
        print(f"{r['archive']:<24}{r['cnt']:>8}{human_size(r['size']):>14}  {r['ts']}")
    print("-" * 70)
    print(f"{'合计':<24}{data['totals']['cnt']:>8}{human_size(data['totals']['size']):>14}")


def print_records(conn, args):
    data = query_records(
        conn,
        filter_archive=args.filter_archive, filter_path=args.filter_path,
        filter_hash=args.filter_hash, min_size=args.min_size,
        sort=args.sort, limit=args.limit, offset=args.offset,
    )
    rows = data["rows"]

    print(f"匹配 {data['matched']} 条，显示 {len(rows)} 条（排序: {data['sort']}）\n")
    print(f"{'原始大小':>10}  {'哈希(前16位)':<17}{'压缩包':<22}文件路径")
    print("-" * 100)
    for r in rows:
        print(f"{human_size(r['size']):>10}  {r['hash'][:16]:<17}{r['archive']:<22}{r['rel_path']}")
    if data["matched"] > len(rows):
        print(f"\n（还有 {data['matched'] - len(rows)} 条未显示，可用 --limit / --offset 翻页）")


def print_duplicates(conn):
    data = get_duplicates(conn)
    if not data["groups"]:
        print("没有内容重复的文件（各文件哈希均唯一）。")
        return
    print(f"发现 {len(data['groups'])} 组内容相同的文件：\n")
    for g in data["groups"]:
        print(f"[{g['hash'][:16]}] {human_size(g['size'])}  共 {g['count']} 个路径:")
        for r in g["rows"]:
            print(f"    {r['rel_path']}   <- {r['archive']}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="查看判重索引数据库（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"索引库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("-l", "--list", action="store_true", help="列出文件记录")
    parser.add_argument("-a", "--archives", action="store_true", help="按压缩包统计")
    parser.add_argument("--duplicates", action="store_true", help="列出内容相同的文件")
    parser.add_argument("--filter-archive", help="按压缩包名过滤（子串匹配）")
    parser.add_argument("--filter-path", help="按文件路径过滤（子串匹配）")
    parser.add_argument("--filter-hash", help="按哈希前缀过滤")
    parser.add_argument("--min-size", type=int, help="只显示不小于该字节数的文件")
    parser.add_argument("--sort", choices=list(SORTS), default="path", help="排序方式（默认 path）")
    parser.add_argument("--limit", type=int, default=100, help="显示条数，0 表示全部（默认 100）")
    parser.add_argument("--offset", type=int, default=0, help="跳过条数，用于翻页")
    args = parser.parse_args()

    conn = open_db(args.db)
    if conn is None:
        sys.exit(f"索引库不存在: {args.db}\n（首次运行 batch_compress.py 后才会生成）")
    try:
        if args.archives:
            print_archives(conn)
        elif args.duplicates:
            print_duplicates(conn)
        elif args.list or args.filter_archive or args.filter_path or args.filter_hash or args.min_size:
            print_records(conn, args)
        else:
            print_summary(conn, args.db)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
