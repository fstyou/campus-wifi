#!/bin/sh
# 安装时保留已有账号配置和会话，不自动启动服务。
set -eu

fail() {
    printf '安装失败：%s\n' "$1" >&2
    exit 1
}

[ "$(id -u)" = 0 ] || fail '请使用 root 用户执行'
for tool in python3 curl ubus iwinfo; do
    command -v "$tool" >/dev/null 2>&1 || fail "缺少依赖：$tool"
done

cd "$(dirname "$0")" || fail '无法进入安装目录'
python3 - <<'PY'
import sys

try:
    import fcntl
    import getpass
    import html
    import json
    import pathlib
    import re
    import select
    import socket
    import subprocess
    import time
    import urllib.parse

    if sys.version_info < (3, 7):
        raise RuntimeError
    compile(pathlib.Path('campus_wifi.py').read_text(encoding='utf-8'), 'campus_wifi.py', 'exec')
except Exception:
    print('安装失败：需要 Python 3.7 及以上版本、完整标准库和有效的主程序文件', file=sys.stderr)
    sys.exit(1)
PY

# 检查并持有与主程序相同的锁，避免覆盖运行中的程序。
python3 - <<'PY'
import fcntl
import os
from pathlib import Path
import shutil
import sys

os.umask(0o077)
try:
    runtime = Path('/tmp/campus-wifi')
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (runtime / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        program_dir = Path('/usr/lib/campus-wifi')
        config_dir = Path('/etc/campus-wifi')
        program_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir.chmod(0o700)
        for source, target, mode in (
            ('campus_wifi.py', program_dir / 'campus_wifi.py', 0o700),
            ('button-login.sh', program_dir / 'button-login.sh', 0o700),
            ('bind_reset.py', program_dir / 'bind_reset.py', 0o700),
            ('campus-wifi.init', Path('/etc/init.d/campus-wifi'), 0o755),
        ):
            temporary = target.with_suffix('.new')
            shutil.copyfile(source, temporary)
            temporary.chmod(mode)
            os.replace(temporary, target)
except BlockingIOError:
    print('安装失败：程序正在运行，请先执行 /etc/init.d/campus-wifi stop', file=sys.stderr)
    sys.exit(1)
except OSError as exc:
    print('安装失败：无法写入程序文件，错误码：' + str(exc.errno), file=sys.stderr)
    sys.exit(1)
print('安装完成，已有配置和会话保持不变。请按说明配置并启动服务')
PY
