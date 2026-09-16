#!/usr/bin/env python3
"""根据 Wi-Fi 关联状态管理校园网认证，认证请求固定通过 eth0。"""

import contextlib
import fcntl
import getpass
import html
import io
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

BASE = Path("/etc/campus-wifi")
RUNTIME = Path("/tmp/campus-wifi")
DEFAULT_PORTAL_URL = "http://10.255.100.2"
PROBE = "http://123.123.123.123/"
ERROR_LOG_INTERVAL = 600


class Problem(Exception):
    """可向用户显示的操作异常。"""


class RepeatedErrorLogger:
    """立即记录新错误，并限制同一持续错误的重复输出。"""

    def __init__(self, interval=ERROR_LOG_INTERVAL):
        self.interval = interval
        self.last_message = None
        self.last_logged_at = None

    def report(self, message, now=None):
        now = time.monotonic() if now is None else now
        if (
            message != self.last_message
            or self.last_logged_at is None
            or now - self.last_logged_at >= self.interval
        ):
            print("本轮操作失败：" + message, flush=True)
            self.last_message = message
            self.last_logged_at = now

    def clear(self):
        self.last_message = None
        self.last_logged_at = None


PRESENCE_LABELS = {"present": "有设备连接", "absent": "无设备连接", "unknown": "无法确定"}


def parse_action(arguments):
    help_text = """用法：python3 campus_wifi.py 命令

可用命令：
  configure  配置校园网账号和密码
  status     查看 Wi-Fi 关联状态，服务运行时也可使用
  login      登录校园网，服务运行时也可使用
  logout     登出校园网，服务运行时也可使用
  run        持续检测并自动登录
  --help     显示此帮助
"""
    if arguments in (["-h"], ["--help"]):
        print(help_text, end="")
        raise SystemExit(0)
    if len(arguments) != 1 or arguments[0] not in {
        "configure",
        "status",
        "login",
        "logout",
        "run",
    }:
        print(help_text, end="", file=sys.stderr)
        raise Problem("请指定一个有效命令")
    return arguments[0]


def normalize_portal_url(value):
    if not isinstance(value, str):
        raise Problem("认证服务器地址必须是文本")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise Problem("认证服务器地址格式无效") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
        or not 1 <= (port or (443 if parsed.scheme == "https" else 80)) <= 65535
    ):
        raise Problem("认证服务器地址应类似 http://10.255.100.2，不能包含路径或参数")
    return value


def portal_origin(value):
    parsed = urlsplit(normalize_portal_url(value))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def load_config(required=True):
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return {}
        raise Problem("请先执行 configure 完成账号配置") from None
    except ValueError:
        raise Problem("配置文件不是有效 JSON") from None
    if not isinstance(cfg, dict):
        raise Problem("配置文件必须是 JSON 对象")
    if not all(isinstance(cfg.get(key), str) and cfg[key] for key in ("username", "password")):
        raise Problem("配置中的账号和密码必须是非空文本")
    if not cfg["password"].isascii():
        raise Problem("密码仅支持 ASCII 字符")
    if not isinstance(cfg.get("service", ""), str):
        raise Problem("服务名称必须是文本")
    cfg["portal_url"] = normalize_portal_url(
        cfg.get("portal_url", DEFAULT_PORTAL_URL)
    )
    interval = cfg.get("check_interval", 5)
    if type(interval) is not int or not 1 <= interval <= 86400:
        raise Problem("检测间隔必须是 1 到 86400 秒之间的整数")
    cfg["check_interval"] = interval
    return cfg


def validate_state(state):
    if state is None:
        return
    if not isinstance(state, dict) or not all(
        isinstance(state.get(key), str) and state[key] for key in ("userIndex", "username")
    ):
        raise Problem("会话文件字段无效，请先检查会话文件")
    if "portal_url" in state:
        normalize_portal_url(state["portal_url"])


def print_status(snapshot):
    print("无线状态：" + PRESENCE_LABELS[snapshot["presence"]])
    print("关联记录数：" + str(snapshot["association_records"]))
    print("已检查的无线接入点：" + ("、".join(snapshot["aps"]) or "无"))
    for record in snapshot["records"]:
        print("关联设备：" + record["interface"] + " / " + record["mac"])
    for message in snapshot["errors"]:
        print("检查异常：" + message)


def command(args, timeout=8):
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Problem("命令无法执行或已超时：" + args[0])
    return p.returncode, p.stdout, p.stderr


def encode(s):
    return quote(str(s), safe="~!*'()-._")


def encode2(s):
    return encode(encode(s))


def rsa_password(password, mac, exponent, modulus):
    # 复现认证网页的反转、零填充和原始 RSA 运算，不能替换为通用填充算法。
    if not password.isascii():
        raise Problem("密码只支持英文字母、数字和 ASCII 符号")
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]+", value)
        for value in (exponent, modulus)
    ):
        raise Problem("认证服务器返回的 RSA 公钥格式无效")
    n, e = int(modulus, 16), int(exponent, 16)
    if not 512 <= n.bit_length() <= 4096 or not 1 < e < 2**32:
        raise Problem("认证服务器返回的 RSA 公钥范围异常")
    chunk = 2 * ((n.bit_length() - 1) // 16)
    value = (password + ">" + (mac or "111111111"))[::-1].encode("ascii")
    value += b"\0" * ((-len(value)) % chunk)
    result = []
    for i in range(0, len(value), chunk):
        c = pow(int.from_bytes(value[i : i + chunk], "little"), e, n)
        result.append(format(c, "x").zfill(max(4, ((c.bit_length() + 15) // 16) * 4)))
    return " ".join(result)


def atomic_json(path, data):
    temp = path.with_suffix(".new")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def wifi_snapshot():
    """动态发现接口，查询异常时不将设备数量视为零。"""
    before = set(os.listdir("/sys/class/net"))
    records, errors, aps = [], [], []
    for name in sorted(before):
        path = Path("/sys/class/net") / name
        try:
            up = int((path / "flags").read_text().strip(), 16) & 1
        except (OSError, ValueError):
            errors.append(name + "：无法读取接口状态")
            continue
        if not up or name in ("lo", "eth0"):
            continue
        rc, info, err = command(["iwinfo", name, "info"])
        if rc:
            if "No such wireless device" in info + err:
                # 有线接口、网桥和隧道不参与设备判断。
                continue
            errors.append(name + "：无线信息查询失败")
            continue
        mode = re.search(r"Mode:\s*(\S+)", info)
        if not mode:
            errors.append(name + "：无法识别无线工作模式")
            continue
        if mode.group(1) == "Client":
            continue
        if mode.group(1) != "Master":
            errors.append(name + "：暂不支持此无线工作模式")
            continue
        aps.append(name)
        rc, out, _ = command(["ubus", "call", "hostapd." + name, "get_clients"])
        try:
            clients = json.loads(out)["clients"] if rc == 0 else None
        except (ValueError, KeyError, TypeError):
            clients = None
        if isinstance(clients, dict) and all(
            isinstance(v, dict) and isinstance(v.get("assoc"), bool) for v in clients.values()
        ):
            for mac, val in clients.items():
                if val["assoc"]:
                    records.append({"interface": name, "mac": mac})
            continue
        # 厂商虚拟接入点可能没有 hostapd 对象，改查驱动关联表。
        rc, out, _ = command(["iwinfo", name, "assoclist"])
        macs = re.findall(r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s", out, re.M)
        if rc == 0 and macs:
            records.extend({"interface": name, "mac": mac.lower()} for mac in macs)
        elif rc == 0 and out.strip() == "No station connected":
            pass
        else:
            errors.append(name + "：无线关联查询失败")
    if set(os.listdir("/sys/class/net")) != before:
        errors.append("采样期间网络接口发生变化")
    # 无线可能正在重启；未发现接入点时将本轮结果标记为未知。
    if not aps:
        errors.append("未发现已启用的无线接入点")
    return {
        "presence": "present" if records else ("unknown" if errors else "absent"),
        "association_records": len(records),
        "records": records,
        "aps": aps,
        "errors": errors,
    }


def portal_url_from_response(text, portal_url):
    # 只提取与配置地址同源的认证页面，不执行服务器返回的 JavaScript。
    expected_scheme, expected_host, expected_port = portal_origin(portal_url)
    candidates = re.findall(
        r"https?://[^\s\x22\x27<>]+/eportal/index\.jsp\?[^\s\x22\x27<>]+",
        text,
        re.I,
    )
    for candidate in candidates:
        candidate = html.unescape(candidate)
        try:
            parsed = urlsplit(candidate)
            candidate_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            continue
        if (
            parsed.scheme.lower() == expected_scheme
            and parsed.hostname
            and parsed.hostname.lower() == expected_host
            and candidate_port == expected_port
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/eportal/index.jsp"
            and "wlanuserip=" in parsed.query
        ):
            return candidate
    return None


class Portal:
    def __init__(self, cfg):
        self.cfg = cfg
        self.portal_url = normalize_portal_url(
            cfg.get("portal_url", DEFAULT_PORTAL_URL)
        )
        self.cookie = RUNTIME / "cookies"
        self.referer = self.portal_url + "/eportal/index.jsp"

    def request(self, url, data=None, headers=False):
        args = [
            "curl",
            "--interface",
            "eth0",
            "--noproxy",
            "*",
            "--connect-timeout",
            "5",
            "--max-time",
            "15",
            "--max-filesize",
            "1048576",
            "-sS",
            "-b",
            str(self.cookie),
            "-c",
            str(self.cookie),
        ]
        if headers:
            args += ["-i"]
        if data is not None:
            args += [
                "-H",
                "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                "-H",
                "Origin: " + self.portal_url,
                "--referer",
                self.referer,
                "--data-binary",
                "@-",
            ]
        args += [url]
        try:
            p = subprocess.run(
                args,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Problem("HTTP 请求失败或超时")
        if p.returncode:
            raise Problem("网络请求失败，curl 返回码：" + str(p.returncode))
        if len(p.stdout) > 1048576:
            raise Problem("认证响应超过大小限制")
        return p.stdout

    def api(self, method, body):
        raw = self.request(
            self.portal_url + "/eportal/InterFace.do?method=" + method,
            body,
        )
        try:
            result = json.loads(raw.lstrip("\ufeff").strip())
        except ValueError:
            raise Problem(method + "：响应不是有效 JSON，字符数：" + str(len(raw)))
        if not isinstance(result, dict):
            raise Problem(method + "：响应数据结构异常")
        return result

    def discover(self):
        return portal_url_from_response(
            self.request(PROBE, headers=True),
            self.portal_url,
        )

    def login(self):
        url = self.discover()
        if not url:
            raise Problem(
                "未获取到新的认证地址，请确认 WAN 口（eth0）已接入校园网，且当前未通过其他方式登录"
            )
        self.referer = url
        self.request(url)
        raw_query = urlsplit(url).query
        info = self.api("pageInfo", "queryString=" + encode(raw_query))
        if info.get("validCodeUrl"):
            raise Problem("认证需要验证码，请先通过浏览器处理")
        mac = re.search(r"(?:^|&)mac=([^&]+)", raw_query, re.I)
        encrypted = rsa_password(
            self.cfg["password"],
            mac.group(1) if mac else "",
            info.get("publicKeyExponent"),
            info.get("publicKeyModulus"),
        )
        fields = {
            "userId": self.cfg["username"],
            "password": encrypted,
            "service": self.cfg.get("service", ""),
            "queryString": raw_query,
            "operatorPwd": "",
            "operatorUserId": "",
            "validcode": "",
            "passwordEncrypt": "true",
        }
        result = self.api("login", "&".join(k + "=" + encode2(v) for k, v in fields.items()))
        if result.get("result") != "success" or not result.get("userIndex"):
            # 不直接输出服务器消息，避免将账号或会话信息写入日志。
            raise Problem("未确认登录成功，请通过浏览器检查账号、服务选项或验证码")
        return {
            "userIndex": result["userIndex"],
            "username": self.cfg["username"],
            "portal_url": self.portal_url,
        }

    def logout(self, state):
        result = self.api("logout", "userIndex=" + encode(state["userIndex"]))
        if result.get("result") != "success":
            raise Problem("未确认登出成功，已保留会话，可手动登出")

    def online(self, state):
        r = self.api("getOnlineUserInfo", "userIndex=" + encode(state["userIndex"]))
        return r.get("result") == "success"


class Engine:
    def __init__(self, portal, state, save):
        self.portal, self.state, self.save = portal, state, save
        # 首次有效采样只建立基准，不把启动或重启视为设备接入、离开。
        self.previous_count = None

    def step(self, count):
        if count is None:
            return
        if self.previous_count is None:
            self.previous_count = count
            return
        previous_count = self.previous_count
        self.previous_count = count
        if count <= previous_count:
            return
        # 先记录新基准；登录失败后，相同数量不会重复提交。
        self.login_once("自动")

    def login_once(self, source):
        if self.state and self.portal.online(self.state):
            print("校园网已登录，无需重复登录", flush=True)
            return
        # 仅在关联数量增加或手动触发时检查、登录，不定期查询或保活。
        state = self.portal.login()
        self.save(state)
        self.state = state
        print(source + "登录成功，会话已保存", flush=True)

    def logout_once(self):
        if not self.state:
            print("没有保存的会话，未发送登出请求", flush=True)
            return
        self.portal.logout(self.state)
        self.save(None)
        self.state = None
        print("登出成功", flush=True)


def request_command(action):
    """等待后台返回实际结果；发送后超时也不另起进程重复操作。"""
    path = RUNTIME / ("reply-" + uuid.uuid4().hex[:12] + ".sock")
    request_id = uuid.uuid4().hex
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.bind(str(path))
            path.chmod(0o600)
            client.settimeout(180)
            payload = json.dumps({"action": action, "id": request_id}).encode()
            client.sendto(payload, str(RUNTIME / "control.sock"))
            while True:
                raw = client.recv(8192)
                reply = json.loads(raw.decode("utf-8"))
                if reply.get("id") != request_id:
                    continue
                if not reply.get("ok"):
                    raise Problem(reply.get("message", "操作失败"))
                print(reply["message"])
                return
    except socket.timeout:
        raise Problem("等待后台结果超时，操作可能仍在进行。请查看日志，请勿立即重复提交") from None
    except (ValueError, KeyError):
        raise Problem("后台响应异常，请查看日志") from None
    except OSError:
        raise Problem("无法与后台通信，请确认服务已启动并已升级至新版") from None
    finally:
        if path.exists():
            path.unlink()


def execute_manual(engine, action, source="手动"):
    """命令行与后台共用一次性操作，不改变自动检测的状态基准。"""
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            if action == "login":
                engine.login_once(source)
            elif action == "logout":
                engine.logout_once()
            else:
                raise Problem("不支持的手动操作")
        return {"ok": True, "message": output.getvalue().strip()}
    except Problem as exc:
        return {"ok": False, "message": str(exc)}
    except OSError as exc:
        return {"ok": False, "message": "文件或系统操作失败，错误码：" + str(exc.errno)}


def execute_status():
    """读取无线状态，并将完整结果交给命令行或后台返回。"""
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            print_status(wifi_snapshot())
        return {"ok": True, "message": output.getvalue().strip()}
    except Problem as exc:
        return {"ok": False, "message": str(exc)}
    except OSError as exc:
        return {"ok": False, "message": "文件或系统操作失败，错误码：" + str(exc.errno)}


def run_service(engine, interval):
    """定时检测 Wi-Fi，并处理本机发送的手动命令。"""
    path = RUNTIME / "control.sock"
    if path.exists():
        path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.setblocking(False)
        next_scan = 0
        previous = None
        error_logger = RepeatedErrorLogger()
        while True:
            timeout = max(0, next_scan - time.monotonic())
            ready, _, _ = select.select([listener], [], [], timeout)
            if ready:
                raw, address = listener.recvfrom(4096)
                try:
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, dict) or request.get("action") not in (
                        "login",
                        "logout",
                        "status",
                    ):
                        continue
                    if not isinstance(request.get("id"), str) or not address:
                        continue
                except (ValueError, UnicodeError):
                    continue
                if request["action"] == "status":
                    result = execute_status()
                else:
                    result = execute_manual(engine, request["action"])
                if request["action"] != "status":
                    print(result["message"], flush=True)
                result["id"] = request["id"]
                try:
                    listener.sendto(json.dumps(result).encode("utf-8"), address)
                except OSError:
                    # 客户端可能已退出；操作已经执行，不重复提交。
                    print("命令已处理，但无法将结果返回终端", flush=True)
                if request["action"] != "status":
                    next_scan = time.monotonic() + interval
            if time.monotonic() >= next_scan:
                try:
                    snap = wifi_snapshot()
                    if snap["presence"] != previous:
                        print("无线状态：" + PRESENCE_LABELS[snap["presence"]], flush=True)
                        previous = snap["presence"]
                    count = (
                        None
                        if snap["presence"] == "unknown"
                        else snap["association_records"]
                    )
                    engine.step(count)
                    error_logger.clear()
                except Problem as exc:
                    error_logger.report(str(exc))
                next_scan = time.monotonic() + interval


def configure():
    if not sys.stdin.isatty():
        raise Problem("请在交互式终端中执行 configure")
    old = load_config(required=False)
    interval = old.get("check_interval", 5)
    portal_url = old.get("portal_url", DEFAULT_PORTAL_URL)
    username = input("请输入校园网账号：").strip()
    password = getpass.getpass("请输入校园网密码（输入时不回显）：")
    if not username or not password or not password.isascii():
        raise Problem("账号和密码不能为空，密码仅支持 ASCII 字符")
    service = input("请输入服务名称（可直接回车留空）：").strip()
    entered_portal_url = input(
        "请输入认证服务器地址（可直接回车使用 " + portal_url + "）："
    ).strip()
    portal_url = normalize_portal_url(entered_portal_url or portal_url)
    atomic_json(
        BASE / "config.json",
        {
            "username": username,
            "password": password,
            "service": service,
            "portal_url": portal_url,
            "check_interval": interval,
        },
    )
    print("配置已保存，尚未发送登录请求")


def main():
    action = parse_action(sys.argv[1:])
    os.umask(0o077)
    for directory in (BASE, RUNTIME):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    with open(RUNTIME / "lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if action in ("login", "logout", "status"):
                request_command(action)
                return
            raise Problem("程序正在运行，请先停止服务再配置账号")
        if action == "configure":
            configure()
            return
        if action == "status":
            print_status(wifi_snapshot())
            return
        cfg = load_config()
        state_path = BASE / "session.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = None
        except ValueError:
            raise Problem("会话文件损坏，请检查后再操作")
        validate_state(state)
        if state and state.get("username") != cfg["username"]:
            raise Problem("保存的会话与当前账号不同，请先处理原账号会话")
        if state and portal_origin(
            state.get("portal_url", DEFAULT_PORTAL_URL)
        ) != portal_origin(cfg["portal_url"]):
            raise Problem("保存的会话属于其他认证服务器，请先使用原地址处理该会话")

        def save(value):
            # 仅在会话变化时写入文件，减少闪存写入并支持重启后恢复。
            atomic_json(state_path, value)

        portal = Portal(cfg)
        engine = Engine(portal, state, save)
        if action in ("login", "logout"):
            result = execute_manual(engine, action)
            if not result["ok"]:
                raise Problem(result["message"])
            print(result["message"])
        else:
            run_service(engine, cfg["check_interval"])


if __name__ == "__main__":
    try:
        main()
    except Problem as exc:
        print("错误：" + str(exc), file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print("错误：文件或系统操作失败，错误码：" + str(exc.errno), file=sys.stderr)
        sys.exit(1)
    except EOFError:
        print("错误：输入已结束，配置未完成", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
