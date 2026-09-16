#!/bin/sh
# 供按键事件分发器调用：只响应不足一秒的短按释放事件。
# 此文件不直接绑定或覆盖 /etc/rc.button 中的系统按键脚本。
[ "${ACTION:-}" = 'released' ] || exit 0
[ "${SEEN:-}" = '0' ] || exit 0

/usr/bin/python3 /usr/lib/campus-wifi/campus_wifi.py login 2>&1 |
    logger -t campus-wifi
