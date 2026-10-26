# -*- coding: utf-8 -*-
"""
====================================================================
 Qwen2-VL 视觉模型 下载器（支持断点续传 + 进度条 + 字节校验）
 - 下载中产生 .part 临时文件，中断后重新运行会自动接着下
 - 网络掉线会自动等待并重试，无需人工干预
 - 全部下载完成后自动按“精确字节数”校验文件大小
 - 只使用 Python 标准库，无需 pip 安装任何东西
====================================================================
"""

import os
import sys
import time
import shutil
import ipaddress
import socket
import urllib.parse
import urllib.request
import urllib.error

# -------------------- 配置区（一般不需要改） --------------------
# 仓库根目录下的 models/（权重永不入库，见 .gitignore 与 docs/MCP_接入指南.md）
DEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# 2B 推荐档（~2.2GB，8GB 显卡富余）；用法：python download_models.py
FILES_2B = [
    {
        "name": "Qwen2-VL-2B-Instruct-Q4_K_M.gguf",   # 主模型（2B）
        "url": "https://hf-mirror.com/bartowski/Qwen2-VL-2B-Instruct-GGUF/resolve/main/Qwen2-VL-2B-Instruct-Q4_K_M.gguf",
        "size": 986047232,
    },
    {
        "name": "mmproj-Qwen2-VL-2B-Instruct-f16.gguf",  # 视觉投影器（必需）
        "url": "https://hf-mirror.com/bartowski/Qwen2-VL-2B-Instruct-GGUF/resolve/main/mmproj-Qwen2-VL-2B-Instruct-f16.gguf",
        "size": 1331656192,
    },
]

# 7B 画质升级档（~5.6GB，8GB 显卡需先停 Ollama 等占卡程序）
# 用法：python download_models.py 7b
FILES_7B = [
    {
        "name": "Qwen2-VL-7B-Instruct-Q4_K_M.gguf",   # 主模型（7B）
        "url": "https://hf-mirror.com/bartowski/Qwen2-VL-7B-Instruct-GGUF/resolve/main/Qwen2-VL-7B-Instruct-Q4_K_M.gguf",
        "size": 4683072672,
    },
    {
        "name": "mmproj-Qwen2-VL-7B-Instruct-f16.gguf",  # 视觉投影器（7B，必需）
        "url": "https://hf-mirror.com/bartowski/Qwen2-VL-7B-Instruct-GGUF/resolve/main/mmproj-Qwen2-VL-7B-Instruct-f16.gguf",
        "size": 1352635904,
    },
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CHUNK_SIZE = 1024 * 1024   # 每次读写 1 MB
MAX_RETRIES = 500          # 单个文件最多自动重试次数
# 只允许从这两个 HTTPS 模型源下载；解析后必须是公网地址（拒绝
# 环回/私网/链路本地/保留/组播），防止被改写成内网或云元数据地址。
ALLOWED_HOSTS = {"hf-mirror.com", "huggingface.co"}
# ----------------------------------------------------------------


def _assert_public_host(host):
    """解析 host 并拒绝任何非公网地址（basic SSRF guard）。"""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addrs = {str(info[4][0]).split("%")[0] for info in infos}
    if not addrs:
        raise ValueError("host %r resolved to no address" % host)
    for raw in addrs:
        addr = ipaddress.ip_address(raw)
        if (not addr.is_global) or addr.is_multicast:
            raise ValueError("host %r resolves to non-public %s"
                             % (host, raw))


def _validate_url(url, initial=True):
    """https + 初始域名白名单 + 解析后公网 IP；重定向终址也要过一遍。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("only https URLs are allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL has no host")
    if initial and host not in ALLOWED_HOSTS:
        raise ValueError("host %r is not in the model-source allowlist"
                         % host)
    _assert_public_host(host)


def fmt_size(n):
    """字节数转成人类可读的字符串"""
    n = float(n)
    if n < 1024:
        return "%d B" % n
    if n < 1024 ** 2:
        return "%.1f KB" % (n / 1024)
    if n < 1024 ** 3:
        return "%.1f MB" % (n / 1024 ** 2)
    return "%.2f GB" % (n / 1024 ** 3)


def fmt_time(s):
    """秒数转成 x时x分x秒"""
    s = int(s)
    if s < 0:
        return "--"
    if s < 60:
        return "%d秒" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%d分%02d秒" % (m, s)
    h, m = divmod(m, 60)
    return "%d时%02d分" % (h, m)


def render_progress(name, done, total, speed):
    """在同一行刷新进度条"""
    if total and total > 0:
        pct = min(1.0, done / total)
        filled = int(50 * pct)
        bar = "#" * filled + "-" * (50 - filled)
        eta = (total - done) / speed if speed > 0 else -1
        line = "\r  %5.1f%% |%s| %s / %s | %s/s | 剩余%s        " % (
            pct * 100, bar, fmt_size(done), fmt_size(total),
            fmt_size(speed), fmt_time(eta))
    else:
        line = "\r  %s 已下载 | %s/s        " % (fmt_size(done), fmt_size(speed))
    sys.stdout.write(line)
    sys.stdout.flush()


def download_one(index, total_count, item):
    """下载单个文件，支持断点续传。成功返回 True，失败返回 False。"""
    name = item["name"]
    url = item["url"]
    expected = item["size"]

    try:
        _validate_url(url, initial=True)
    except ValueError as exc:
        print("[安全拒绝] %s: %s" % (name, exc))
        return False

    final_path = os.path.join(DEST_DIR, name)
    part_path = final_path + ".part"

    # 如果正式文件已经存在且大小正确，直接跳过
    if os.path.exists(final_path):
        actual = os.path.getsize(final_path)
        if actual == expected:
            print("[%d/%d] [跳过] %s 已存在且大小正确（%s 字节）"
                  % (index, total_count, name, format(actual, ",")))
            return True
        else:
            print("[%d/%d] [警告] %s 已存在但大小不对（%s != %s），重新下载"
                  % (index, total_count, name, format(actual, ","),
                     format(expected, ",")))
            os.remove(final_path)

    # 异常的 .part（比完整文件还大）直接删掉
    if os.path.exists(part_path) and os.path.getsize(part_path) >= expected:
        os.remove(part_path)

    for attempt in range(1, MAX_RETRIES + 1):
        existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        headers = {"User-Agent": USER_AGENT}
        mode = "wb"
        start_from = 0
        if existing > 0:
            headers["Range"] = "bytes=%d-" % existing

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.status

                if existing > 0 and status == 206:
                    # 服务器同意续传，解析 Content-Range: bytes start-end/total
                    content_range = resp.headers.get("Content-Range", "")
                    total = int(content_range.split("/")[-1])
                    mode = "ab"
                    start_from = existing
                elif status == 200:
                    # 服务器返回完整文件（从头开始）
                    total = int(resp.headers.get("Content-Length", -1))
                    mode = "wb"
                    start_from = 0
                else:
                    raise IOError("意外的 HTTP 状态码: %d" % status)

                if total != expected:
                    print("\n  [警告] 服务器文件大小 %s 与预期 %s 不一致，仍继续下载"
                          % (format(total, ","), format(expected, ",")))

                if attempt == 1 and start_from > 0:
                    print("  已发现未完成的下载，从 %s 处继续……" % fmt_size(start_from))
                elif attempt > 1:
                    print("  第 %d 次继续下载，从 %s 处接着下……"
                          % (attempt, fmt_size(start_from)))

                downloaded = start_from
                t_start = time.time()
                last_draw = 0.0
                with open(part_path, mode) as f:
                    while True:
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_draw >= 0.3:
                            elapsed = now - t_start
                            got = downloaded - start_from
                            speed = got / elapsed if elapsed > 0 else 0
                            render_progress(name, downloaded, total, speed)
                            last_draw = now

            actual = os.path.getsize(part_path)
            if actual == expected:
                os.replace(part_path, final_path)
                sys.stdout.write("\r")
                print("[%d/%d] [完成] %s —— %s 字节，大小校验通过！"
                      % (index, total_count, name, format(actual, ",")))
                return True
            else:
                sys.stdout.write("\r")
                print("\n  [异常] 本次结束时大小为 %s，预期 %s，准备重试"
                      % (format(actual, ","), format(expected, ",")))
        except KeyboardInterrupt:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, IOError) as e:
            sys.stdout.write("\r")
            print("\n  [第 %d 次中断] %s: %s" % (attempt, type(e).__name__, e))
            wait = min(30, 2 * attempt)
            print("  已下载的部分已保留，%d 秒后自动续传……"
                  "（也可以直接关掉窗口，下次双击继续）" % wait)
            time.sleep(wait)

    print("  [失败] %s 重试次数已达上限，请重新运行本脚本继续。" % name)
    return False


def main():
    # Windows 控制台尽量用 UTF-8 输出，避免中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    files = FILES_2B
    label = "2B 推荐档（~2.2GB）"
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("7b", "7"):
        files = FILES_7B
        label = "7B 画质升级档（~5.6GB，8GB 显卡先停 Ollama）"

    print("=" * 66)
    print("  Qwen2-VL 视觉模型下载器（断点续传 / 进度条 / 字节校验）")
    print("  档位: %s（传参数 7b 可切换 7B 档）" % label)
    print("  保存目录: %s" % DEST_DIR)
    print("=" * 66)

    # 1. 自动创建目录
    os.makedirs(DEST_DIR, exist_ok=True)

    # 2. 检查磁盘剩余空间（多留 500 MB 余量）
    need = sum(f["size"] for f in files)
    free = shutil.disk_usage(DEST_DIR).free
    print("  需要空间: %s，当前剩余: %s" % (fmt_size(need), fmt_size(free)))
    if free < need + 500 * 1024 * 1024:
        print("  [错误] 磁盘空间不足，请清理后重试。")
        input("按回车键退出…")
        sys.exit(1)

    # 3. 逐个下载
    results = []
    try:
        for i, item in enumerate(files, start=1):
            print("-" * 66)
            print("[%d/%d] %s（预期 %s 字节）"
                  % (i, len(files), item["name"], format(item["size"], ",")))
            ok = download_one(i, len(files), item)
            results.append((item["name"], ok))
    except KeyboardInterrupt:
        print("\n\n你手动中断了下载。已下载的部分已保存为 .part 文件，")
        print("重新运行本脚本即可从断点继续，不会从头开始。")
        input("按回车键退出…")
        sys.exit(1)

    # 4. 汇总结果
    print("=" * 66)
    all_ok = True
    for name, ok in results:
        mark = "[成功]" if ok else "[失败]"
        print("  %s %s" % (mark, name))
        all_ok = all_ok and ok

    if all_ok:
        print("\n  全部文件下载完成并通过精确字节校验，模型可以使用了！")
    else:
        print("\n  有文件未完成，重新运行本脚本即可继续下载。")
    print("=" * 66)
    input("按回车键退出…")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
