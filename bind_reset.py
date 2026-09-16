#!/usr/bin/env python3
"""为已核对的 GL.iNet 重置键脚本追加短按登录，保留厂商原有逻辑。"""

import hashlib
import json
import os
import sys
from pathlib import Path

TARGET = Path("/etc/rc.button/reset")
BACKUP = Path("/etc/campus-wifi/reset-button-backup.json")
MARKER = "# campus-wifi：短按登录"
ANCHOR = "\treset_btn_released\n"
HOOK = """
\t# campus-wifi：短按登录
\tif [ "${SEEN:-}" = "0" ]; then
\t\t/usr/lib/campus-wifi/button-login.sh &
\tfi
"""


class BindError(Exception):
    """按键绑定失败。"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_file(path, data, mode):
    temporary = path.with_name(path.name + ".campus-new")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def patch_script(original):
    try:
        source = original.decode("utf-8")
    except UnicodeError:
        raise BindError("按键脚本编码异常，未修改") from None
    # 仅接受本次已核对的厂商结构，不对未知固件猜测插入位置。
    required = (
        "reset_btn_pressed",
        "released)",
        "factory_reset",
        "reset_network &",
        '[ "$SEEN" -ge 20 ]',
        '[ "$SEEN" -ge 8 ]',
        '[ "$SEEN" -ge 3 ]',
    )
    if MARKER in source or source.count(ANCHOR) != 1 or not all(x in source for x in required):
        raise BindError("按键脚本与已支持的厂商结构不符，未修改")
    return source.replace(ANCHOR, ANCHOR + HOOK, 1).encode("utf-8")


def bind(action, target=TARGET, backup=BACKUP):
    if target.is_symlink():
        raise BindError("按键脚本是符号链接，可能由其他插件管理，未修改")
    original = target.read_bytes()
    record = None
    if backup.exists():
        try:
            record = json.loads(backup.read_text(encoding="utf-8"))
            saved = bytes.fromhex(record["original"])
            if digest(saved) != record["original_hash"] or not isinstance(record["mode"], int):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise BindError("按键备份数据异常，未修改") from None
    if action == "status":
        if record and digest(original) == record["patched_hash"]:
            return "已绑定：短按不足一秒，松开后提交登录请求"
        return "当前未绑定，或脚本已被其他程序修改"
    if action == "disable":
        if record is None:
            return "没有备份，未修改按键脚本"
        if digest(original) == record["original_hash"]:
            return "已经恢复原按键脚本"
        if digest(original) != record["patched_hash"]:
            raise BindError("按键脚本在绑定后发生变化，为避免覆盖其他改动，未恢复")
        write_file(target, saved, record["mode"])
        return "已恢复绑定前的按键脚本"
    if record:
        if digest(original) == record["patched_hash"]:
            return "按键已经绑定，无需重复操作"
        if digest(original) != record["original_hash"]:
            raise BindError("当前脚本与备份不一致，未修改")
    patched = patch_script(original)
    mode = target.stat().st_mode & 0o777
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "original": original.hex(),
        "original_hash": digest(original),
        "patched_hash": digest(patched),
        "mode": mode,
    }
    write_file(backup, json.dumps(record).encode("utf-8"), 0o600)
    write_file(target, patched, mode)
    return "绑定完成：短按重置键不足一秒并松开，提交一次校园网登录请求"


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("enable", "disable", "status"):
        raise BindError("用法：python3 bind_reset.py enable 或 disable 或 status")
    if os.geteuid() != 0:
        raise BindError("请使用 root 用户执行")
    os.umask(0o077)
    if sys.argv[1] == "enable" and not Path("/usr/lib/campus-wifi/button-login.sh").is_file():
        raise BindError("请先安装新版校园网程序")
    print(bind(sys.argv[1]))


if __name__ == "__main__":
    try:
        main()
    except BindError as error:
        print("错误：" + str(error), file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print("错误：文件操作失败，错误码：" + str(error.errno), file=sys.stderr)
        sys.exit(1)
