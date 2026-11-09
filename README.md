# compress_archive

批量压缩/解压归档工具，基于 **Bandizip 命令行（bz.exe）** 实现，支持按体积自动分包、加密压缩、包完整性校验、失败自动重试、可选的基于内容哈希的判重（避免重复打包已归档文件）。

提供 **Web 前端**（浏览器/桌面窗口）与 **命令行** 两种使用方式；前端化后所有可配置项（目标目录、密码、分包上限等）均可在页面中直接输入。

## 目录结构

| 文件 | 说明 |
| --- | --- |
| `app.py` | Web 服务主程序：静态页面 + REST API + SSE 实时日志/进度；可选 pywebview 桌面壳（不可用时自动降级为浏览器打开） |
| `index.html` | 前端页面（单文件、深色主题）：压缩 / 清理 / 解压 / 索引四个 Tab |
| `启动工具.bat` | Windows 一键启动脚本（自动探测 Python、兼容网络共享盘 UNC 路径） |
| `dedup_clean.py` | 清理工具：孤立 0 字节清理 + 全量内容哈希去重（并行，不再比文件名/大小/时间）；重复副本与空文件**直接删除进 Windows 回收站**（可恢复），并产出供压缩复用的临时 hash 缓存 |
| `batch_compress.py` | 压缩主程序：扫描目录 → 复用清理阶段的临时 hash 缓存判重（缺口现场补算）→ 分包 → 压缩 → 校验 → 写索引/删源文件 |
| `hash_cache.py` | 内容 hash 能力复用模块：BLAKE2b-256 实现 + 并行补算 + 临时缓存原子读写，清理与压缩共享 |
| `batch_decompress.py` | 解压程序：批量解压 `compressed_output` 下的 `.7z` 并可选删除压缩包 |
| `inspect_index.py` | 只读查看判重索引库（概览 / 按包统计 / 查重复内容） |
| `<目标目录末层>_dedup_index.db` | SQLite 判重索引，仅在开启判重入库时随运行自动生成（如目标目录为 `D:\ll` 时为 `ll_dedup_index.db`） |

## 环境要求

- Python 3.10+
- 已安装 [Bandizip](https://www.bandisoft.com/bandizip/)（默认安装目录 `C:\Program Files\Bandizip`）
- 可选：`pywebview`（`pip install pywebview`），安装后以前端模式启动会打开原生桌面窗口；未安装则自动改用浏览器

## 快速开始

### 方式一：Web 前端（推荐）

1. 双击 `启动工具.bat`（或命令行执行 `python app.py`），自动打开前端页面（默认 `127.0.0.1` 随机端口）。
2. 在页面中填写目标目录等参数，点击对应按钮即可：

   | Tab | 功能 |
   | --- | --- |
   | 压缩 | 配置 Bandizip 目录、密码、分包上限、校验/哈希线程数、是否写索引、压缩后是否删源文件 |
   | 清理 | 内容哈希去重 + 孤立 0 字节清理；可勾选「预览模式」先看结果；删除一律进 Windows 回收站 |
   | 解压 | 批量解压并可选删除压缩包 |
   | 索引 | 选择索引库后按概览 / 按包 / 过滤 / 分组四种模式查询 |

3. 任务为后台运行：日志与进度经 SSE 实时推送，「停止任务」可取消当前任务。
4. 关闭启动用的命令行窗口即退出程序。

前端启动参数：`python app.py [--gui auto|webview|browser] [--port 0]`（`--port 0` 表示随机端口；`--gui` 可选原生窗口/强制浏览器）。

### 方式二：命令行

1. （可选）调整脚本顶部的默认配置项，例如 Bandizip 安装目录、压缩包密码、单包体积上限：

   ```python
   BANDIZIP_DIR = r"C:\Program Files\Bandizip"      # Bandizip 安装目录
   PASSWORD     = "xxxx"                            # 压缩包密码
   MAX_PACKAGE_SIZE = int(3.9 * 1024**3)            # 单个包体积上限
   ```

2. **先清理，再压缩**（两步分开执行）：

   ```bash
   # 第一步：内容哈希去重 + 孤立 0 字节清理（先预览确认，再去掉 --preview 执行）
   python dedup_clean.py "D:\ll" --preview
   python dedup_clean.py "D:\ll"

   # 第二步：压缩归档
   python batch_compress.py "D:\ll"                  # 默认：开启判重入库，压缩后删除源文件
   python batch_compress.py "D:\ll" --no-hash-index  # 关闭判重入库
   python batch_compress.py "D:\ll" --keep-source    # 保留源文件，不删除
   python batch_compress.py --help                   # 查看参数说明
   ```

   - 位置参数 `target_dir`：待处理的源目录；不传则使用脚本内的 `DEFAULT_TARGET_DIR`。
   - `batch_compress.py` 开关 `--no-hash-index`：关闭哈希判重与索引入库；默认开启。
   - `batch_compress.py` 开关 `--keep-source`：压缩并校验通过后保留源文件；默认删除源文件。
   - 压缩包输出目录固定为 `<target_dir>/compressed_output`。

3. 查看判重索引：

   ```bash
   python inspect_index.py                        # 概览
   python inspect_index.py --archives             # 按压缩包统计
   python inspect_index.py -l --filter-path 电影  # 按路径过滤列出
   python inspect_index.py --duplicates           # 内容重复的不同路径
   ```

4. 解压：

   ```bash
   python batch_decompress.py
   ```

## 工作流程（dedup_clean.py，库前清理）

在压缩前对目标目录执行重复与断传残留清理，**删除的文件一律进入 Windows 回收站（可恢复）**，不做任何物理删除。扫描范围排除 `compressed_output`，也不扫描 hash 缓存自身。

1. **孤立 0 字节清理**：断传失败残留的 0 字节文件一律删除进回收站；`(N)` 后缀文件与普通文件同等对待，无豁免。
2. **全量内容哈希去重**：对全部剩余文件并行计算 BLAKE2b-256 哈希（不再比对文件名/大小/时间），同目录内哈希相同的视为重复，保留一个（优先保留不带 `(N)` 后缀的文件名），其余副本删除进回收站。
3. **产出临时 hash 缓存**：将保留文件的哈希写盘为缓存文件 `<目标目录末层>.hash_cache.json`，供压缩判重阶段**直接复用**（能力复用，压缩不再重复计算）。

所有输出带 `    ` 前缀缩进，最后汇总「删除到回收站 N 个」。`--preview` 模式下只打印 `[预览]` 标记的操作清单，不改动任何文件（前端清理页的「预览模式」默认**关闭**，如需预览请手动勾选）。

> 说明：不再做 `A.mp4 / A (2).mp4` 成对人工判定，也不做连号序列保护，`(N)` 后缀文件与普通文件同等参与去重与 0 字节清理。

## 工作流程（batch_compress.py）

1. **扫描排序**：收集 `target_dir` 下的文件与子目录（排除 `compressed_output` 与临时 hash 缓存，避免把输出/缓存重新打包），按创建时间 `ctime` 排序，保证打包顺序稳定。
2. **读取清理阶段的临时 hash 缓存**：直接复用 `<目标目录末层>.hash_cache.json` 中的哈希；清理后新增、缓存缺失的文件**现场补算**（并行）。
3. **判重过滤**（可通过 `--no-hash-index` 关闭）：仅按**内容 BLAKE2b-256 哈希**与索引记录比对，不再比文件大小/名称/mtime。
   - 顶层文件按文件级判重，顶层目录内部文件全部命中才整目录跳过。
   - 已在索引中的残留条目会被直接清理（安全，因为索引只记录校验通过的包）；指定 `--keep-source` 时跳过清理。
   - 含无法读取文件的条目本轮跳过，不做处理。
   - 关闭开关时跳过本步骤：不比对索引，所有项目直接进入分包。
4. **按体积分包**：按排序结果顺序累加，超过 `MAX_PACKAGE_SIZE` 即切分为新包；单个超限项独立成包。
5. **压缩**：`bz c -p:<password> -y`，输出为 `compressed_output/YYYYMMDD_序号.7z`。失败清理坏包并重试，最多 `MAX_RETRY` 次。
6. **校验**：压缩线程将结果放入队列，校验线程执行 `bz t` 完整性/密码校验。
   - 校验通过：**先写索引，再删除源文件**（索引丢失比文件残留更严重）；指定 `--keep-source` 时跳过删除源文件。
   - 校验失败：删除坏包，回退到重试队列重新压缩，直到达到重试上限。
7. **重试收敛**：主线程等待校验队列清空，取出所有重试项重新压缩，循环直至无新重试，最后向校验线程投放哨兵退出。
8. **消费完清理缓存**：压缩结束后删除临时 hash 缓存文件（已复用完毕，不留长期残留）。

## 判重索引（SQLite）

仅在 `--no-hash-index` 未指定（即默认开启）时创建与读写。

表 `packed_files`：

| 字段 | 说明 |
| --- | --- |
| `rel_path` | 相对 `TARGET_DIR` 的路径（主键） |
| `size` | 文件大小（字节） |
| `mtime_ns` | 修改时间（纳秒） |
| `hash` | BLAKE2b-256 十六进制摘要 |
| `archive` | 所在压缩包文件名 |
| `packed_at` | 打包时间 |

索引库路径 = **代码目录 + `target_dir` 末层目录名 + `_dedup_index.db`**，因此切换不同项目目录会各自生成独立索引，互不干扰。

## 命令行参数

### dedup_clean.py

| 参数 | 说明 |
| --- | --- |
| `target_dir` | 位置参数，待清理的源目录；缺省时使用脚本内 `DEFAULT_TARGET_DIR`（默认 `F:\ll`） |
| `--preview` | 仅预览将执行的操作，不实际删除任何文件 |
| `-h, --help` | 显示帮助 |

### batch_compress.py

| 参数 | 说明 |
| --- | --- |
| `target_dir` | 位置参数，待压缩的源目录；缺省时使用脚本内 `DEFAULT_TARGET_DIR`（默认 `F:\ll`） |
| `--no-hash-index` | 关闭哈希判重与索引入库；默认开启 |
| `--keep-source` | 压缩并校验通过后保留源文件；默认删除源文件 |
| `-h, --help` | 显示帮助 |

## 注意事项

- **`dedup_clean.py` / 前端「清理」的删除放入 Windows 回收站**：重复副本与孤立 0 字节文件删除后可回收站恢复，不做物理删除、不留归档目录。首次运行建议先勾选预览确认结果。
- 全部清理逻辑仅两步：内容哈希去重（同目录先比大小再比哈希）+ 孤立 0 字节清理；不做 `A.mp4 / A (2).mp4` 成对判定，`(N)` 后缀文件与普通文件同等对待。
- `batch_compress.py` 不处理任何去重/清碎清理，请在压缩前先运行 `dedup_clean.py`；`compressed_output` 会被压缩程序排除。
- `batch_decompress.py` 中的 `SOURCE_DIR` / `OUTPUT_DIR` 目前为示例路径，使用前请改成实际路径（或在前端「解压」页填写）。
- `inspect_index.py` 的默认索引库名是 `_dedup_index.db`；若你使用的是按项目命名的新索引库，请通过 `--db <路径>` 指定，例如：

  ```bash
  python inspect_index.py --db ll_dedup_index.db
  ```

- `启动工具.bat` 要求全部内容保持纯 ASCII 英文（避免 cmd 以 GBK 解码 UTF-8 中文导致乱码报错）；Python 路径自动探测，无需硬编码。
