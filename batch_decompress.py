import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

# ==================== 配置项（用于 CLI main 的默认值） ====================
SOURCE_DIR = r"C:\Users\jiale\Downloads\compressed_output"
OUTPUT_DIR = r"C:\Users\jiale\Downloads"
DEFAULT_BANDIZIP_DIR = r"C:\Program Files\Bandizip"
DEFAULT_MAX_RETRY = 3
DEFAULT_DELETE_AFTER_EXTRACT = True  # 解压成功后是否删除7z包
# ================================================

_print_lock = threading.Lock()


def _default_log_cb(text, end="\n"):
    with _print_lock:
        print(text, end=end)


# 当前日志回调，由 run_decompress 设置；默认为 print，保证 CLI 兼容
_LOG_CB = _default_log_cb


def safe_print(*args, **kwargs):
    """统一经日志回调输出（text 拼接 + end 透传）"""
    end = kwargs.pop("end", "\n")
    text = " ".join(str(a) for a in args)
    _LOG_CB(text, end=end)


def find_bz_exe(bandizip_dir):
    bz_dir = Path(bandizip_dir)
    for name in ("bz.exe", "Bandizip.exe"):
        exe = bz_dir / name
        if exe.exists():
            return str(exe)
    return None


def extract_archive(bz_path, archive_path, output_dir, password):
    cmd = [
        bz_path,
        "x",
        f"-p:{password}",
        "-y",
        f"-o:{output_dir}",
        str(archive_path.resolve()),
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return True, "解压成功"
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else (e.stdout.strip() if e.stdout else "密码错误或压缩包损坏")
        return False, f"解压失败: {err_msg}"


def test_archive(bz_path, archive_path, password):
    cmd = [
        bz_path,
        "t",
        "-y",
        f"-p:{password}",
        str(archive_path.resolve()),
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return True
    except subprocess.CalledProcessError:
        return False


def run_decompress(cfg, log_cb=_default_log_cb, stop_event=None):
    """
    批量解压主流程（可复用入口，供 GUI/前端调用）。

    cfg 字段：
        source_dir            压缩包源目录（含 .7z）
        output_dir            解压输出目录
        bandizip_dir          Bandizip 安装目录
        password              压缩包密码（可为空串）
        max_retry             单个包解压失败最大重试次数
        delete_after_extract  解压成功后是否删除 7z 包

    返回: {"success", "fail", "total", "output_dir"}
    """
    global _LOG_CB
    _LOG_CB = log_cb

    source_dir = cfg["source_dir"]
    output_dir = cfg.get("output_dir") or os.path.dirname(source_dir)
    bandizip_dir = cfg.get("bandizip_dir") or DEFAULT_BANDIZIP_DIR
    password = cfg.get("password") or ""
    max_retry = cfg.get("max_retry") or DEFAULT_MAX_RETRY
    delete_after_extract = cfg.get("delete_after_extract", DEFAULT_DELETE_AFTER_EXTRACT)

    result = {"success": 0, "fail": 0, "total": 0, "output_dir": output_dir}

    source_path = Path(source_dir)
    output_path = Path(output_dir)

    if not source_path.exists():
        safe_print(f"错误: 源目录不存在 {source_dir}")
        result["fail"] = 1
        return result

    bz_exe = find_bz_exe(bandizip_dir)
    if not bz_exe:
        safe_print(f"错误: 未找到 Bandizip，请确认安装目录: {bandizip_dir}")
        result["fail"] = 1
        return result

    output_path.mkdir(parents=True, exist_ok=True)

    archives = sorted(source_path.glob("*.7z"))
    if not archives:
        safe_print("源目录下没有 .7z 压缩包。")
        return result

    result["total"] = len(archives)
    safe_print(f"找到 {len(archives)} 个压缩包，开始解压到 {output_dir} ...\n")

    success_count = 0
    fail_count = 0

    for idx, archive in enumerate(archives, start=1):
        if stop_event is not None and stop_event.is_set():
            safe_print(f"任务已停止，剩余 {len(archives) - idx + 1} 个压缩包未处理")
            break
        safe_print(f"[{idx}/{len(archives)}] 正在解压: {archive.name} ...", end="", flush=True)

        attempt = 0
        ok = False

        while attempt < max_retry:
            if stop_event is not None and stop_event.is_set():
                break
            attempt += 1
            ok, msg = extract_archive(bz_exe, archive, output_path, password)
            if ok:
                break
            if attempt < max_retry:
                safe_print(f"\n       尝试 {attempt}/{max_retry} 失败，重试...", end="", flush=True)

        if ok:
            safe_print(" 成功")
            if delete_after_extract:
                try:
                    archive.unlink()
                    safe_print(f"    已删除压缩包: {archive.name}")
                except Exception as e:
                    safe_print(f"    删除压缩包失败: {e}")
            success_count += 1
        else:
            if stop_event is not None and stop_event.is_set():
                safe_print(" 已停止")
            else:
                safe_print(f" 失败 - {msg}")
            fail_count += 1

    result["success"] = success_count
    result["fail"] = fail_count

    safe_print("\n" + "=" * 60)
    safe_print(f"处理完成！共 {result['total']} 个压缩包:")
    safe_print(f"  - 成功: {success_count} 个")
    safe_print(f"  - 失败: {fail_count} 个")
    safe_print(f"解压目录: {output_dir}")
    return result


def main():
    cfg = {
        "source_dir": SOURCE_DIR,
        "output_dir": OUTPUT_DIR,
        "bandizip_dir": DEFAULT_BANDIZIP_DIR,
        "password": "",  # 默认不加密；CLI 如需密码请在此填写或在前端页面设置
        "max_retry": DEFAULT_MAX_RETRY,
        "delete_after_extract": DEFAULT_DELETE_AFTER_EXTRACT,
    }
    run_decompress(cfg, log_cb=_default_log_cb)


if __name__ == "__main__":
    main()
