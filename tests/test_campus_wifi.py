"""使用虚构数据测试编码、认证流程和 Wi-Fi 状态机，不连接真实校园网。"""

import contextlib
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campus_wifi as app


class EncodingTests(unittest.TestCase):
    def test_reference_rsa_vectors(self):
        vectors = json.loads(Path(__file__).with_name("rsa_vectors.json").read_text())
        for case in vectors["cases"]:
            with self.subTest(length=len(case["password"])):
                self.assertEqual(
                    app.rsa_password(
                        case["password"], case["mac"], vectors["exponent"], vectors["modulus"]
                    ),
                    case["encrypted"],
                )

    def test_invalid_key_and_password(self):
        for values in [("中文", "abc", "10001", "ff"), ("test", "abc", {}, None)]:
            with self.assertRaises(app.Problem):
                app.rsa_password(*values)

    def test_double_encoding(self):
        self.assertEqual(app.encode2("a=b&x=%20"), "a%253Db%2526x%253D%252520")

    def test_redirect_formats(self):
        url = "http://10.255.100.2/eportal/index.jsp?wlanuserip=new&mac=abc"
        for response in [
            f"<script>top.self.location.href='{url}'</script>",
            f"Location: {url}\r\n",
        ]:
            self.assertEqual(
                app.portal_url_from_response(response, "http://10.255.100.2"),
                url,
            )
        self.assertIsNone(
            app.portal_url_from_response(
                "http://example.org/eportal/index.jsp?wlanuserip=x",
                "http://10.255.100.2",
            )
        )

    def test_redirect_uses_configured_origin(self):
        url = "https://portal.example.edu:8443/eportal/index.jsp?wlanuserip=new"
        self.assertEqual(
            app.portal_url_from_response(url, "https://portal.example.edu:8443/"),
            url,
        )
        self.assertIsNone(
            app.portal_url_from_response(url, "https://other.example.edu:8443")
        )
        self.assertEqual(
            app.portal_origin("http://portal.example.edu"),
            app.portal_origin("http://PORTAL.EXAMPLE.EDU:80/"),
        )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.patcher = patch.object(app, "BASE", self.base)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_config(self, **updates):
        data = {"username": "test", "password": "dummy"}
        data.update(updates)
        (self.base / "config.json").write_text(json.dumps(data))

    def test_legacy_config_uses_new_interval(self):
        self.write_config(idle_seconds=5)
        self.assertEqual(app.load_config()["check_interval"], 5)
        self.assertEqual(app.load_config()["portal_url"], app.DEFAULT_PORTAL_URL)

    def test_invalid_interval(self):
        for interval in [0, True, "60", -1]:
            self.write_config(check_interval=interval)
            with self.assertRaises(app.Problem):
                app.load_config()

    def test_configure_preserves_interval(self):
        self.write_config(check_interval=30)
        with patch.object(sys.stdin, "isatty", return_value=True), patch(
            "builtins.input", side_effect=["new", "", "https://portal.example.edu/"]
        ), patch.object(app.getpass, "getpass", return_value="dummy"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            app.configure()
        self.assertEqual(app.load_config()["check_interval"], 30)
        self.assertEqual(
            app.load_config()["portal_url"],
            "https://portal.example.edu",
        )
        self.assertNotIn("idle_seconds", app.load_config())

    def test_invalid_portal_url(self):
        for portal_url in [
            1,
            "10.255.100.2",
            "ftp://10.255.100.2",
            "http://user:pass@10.255.100.2",
            "http://10.255.100.2/eportal",
            "http://10.255.100.2/?x=1",
        ]:
            self.write_config(portal_url=portal_url)
            with self.subTest(portal_url=portal_url), self.assertRaises(app.Problem):
                app.load_config()

    def test_atomic_save_permissions(self):
        path = self.base / "session.json"
        app.atomic_json(path, None)
        self.assertIsNone(json.loads(path.read_text()))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_session(self):
        for state in [[], {}, {"userIndex": 1, "username": "test"}]:
            with self.assertRaises(app.Problem):
                app.validate_state(state)
        with self.assertRaises(app.Problem):
            app.validate_state(
                {
                    "userIndex": "test",
                    "username": "test",
                    "portal_url": "http://10.255.100.2/eportal",
                }
            )


class RepeatedErrorLoggerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.logger = app.RepeatedErrorLogger(interval=600)

    def lines(self):
        return self.output.getvalue().splitlines()

    def test_same_error_is_limited_to_once_per_interval(self):
        self.logger.report("测试错误", now=0)
        self.logger.report("测试错误", now=599)
        self.logger.report("测试错误", now=600)
        self.assertEqual(
            self.lines(),
            ["本轮操作失败：测试错误", "本轮操作失败：测试错误"],
        )

    def test_different_error_is_logged_immediately(self):
        self.logger.report("错误一", now=0)
        self.logger.report("错误二", now=1)
        self.assertEqual(
            self.lines(),
            ["本轮操作失败：错误一", "本轮操作失败：错误二"],
        )

    def test_recovered_error_is_logged_as_new(self):
        self.logger.report("测试错误", now=0)
        self.logger.clear()
        self.logger.report("测试错误", now=1)
        self.assertEqual(len(self.lines()), 2)


class WifiTests(unittest.TestCase):
    def snapshot(self, names, command):
        with patch.object(app.os, "listdir", return_value=names), patch.object(
            Path, "read_text", return_value="0x1"
        ), patch.object(app, "command", side_effect=command):
            return app.wifi_snapshot()

    def test_discovers_new_ap_and_excludes_upstream(self):
        def command(args):
            if args[0] == "iwinfo" and args[2] == "info":
                if args[1] == "eth1":
                    return 1, "", "No such wireless device"
                return 0, "Mode: " + ("Client" if args[1] == "upstream" else "Master"), ""
            return (
                0,
                json.dumps(
                    {"clients": {"aa:bb:cc:dd:ee:ff": {"assoc": True, "authorized": False}}}
                ),
                "",
            )

        result = self.snapshot(["eth0", "eth1", "upstream", "new_ap"], command)
        self.assertEqual(result["presence"], "present")
        self.assertEqual(result["aps"], ["new_ap"])
        self.assertEqual(result["association_records"], 1)

    def test_failed_fallback_is_unknown(self):
        def command(args):
            if args[-1] == "info":
                return 0, "Mode: Master", ""
            if args[0] == "ubus":
                return 1, "", ""
            return 0, "No information available", ""

        self.assertEqual(self.snapshot(["ap"], command)["presence"], "unknown")

    def test_empty_fallback_is_absent(self):
        def command(args):
            if args[-1] == "info":
                return 0, "Mode: Master", ""
            if args[0] == "ubus":
                return 1, "", ""
            return 0, "No station connected", ""

        self.assertEqual(self.snapshot(["ap"], command)["presence"], "absent")

    def test_no_ap_is_unknown(self):
        self.assertEqual(self.snapshot(["eth0"], Mock())["presence"], "unknown")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.portal = Mock()
        self.portal.login.return_value = {
            "userIndex": "test",
            "username": "dummy",
        }
        self.portal.online.return_value = True
        self.save = Mock()
        self.engine = app.Engine(self.portal, None, self.save)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def count(self, count):
        self.engine.step(count)

    def test_all_requested_transitions(self):
        for before, after in [
            (0, 1),
            (0, 3),
            (1, 0),
            (3, 0),
            (2, 3),
            (1, 3),
            (0, 0),
            (3, 1),
            (2, 2),
        ]:
            with self.subTest(before=before, after=after):
                self.portal.reset_mock()
                self.engine = app.Engine(self.portal, None, self.save)
                self.count(before)
                if before:
                    self.engine.state = self.portal.login.return_value
                self.count(after)
                self.assertEqual(
                    self.portal.login.call_count,
                    int(after > before and before == 0),
                )
                self.assertEqual(
                    self.portal.online.call_count,
                    int(after > before and before > 0),
                )
                if after <= before:
                    self.assertEqual(self.portal.mock_calls, [])

    def test_manual_login_while_zero_preserves_baseline(self):
        self.count(0)
        self.assertTrue(app.execute_manual(self.engine, "login")["ok"])
        self.portal.reset_mock()
        for _ in range(10):
            self.count(0)
        self.assertEqual(self.portal.mock_calls, [])
        self.assertIsNotNone(self.engine.state)

    def test_lost_session_reconnects_only_after_count_increases(self):
        self.count(0)
        self.count(1)
        self.portal.online.return_value = False
        self.portal.reset_mock()
        self.count(1)
        self.assertEqual(self.portal.mock_calls, [])
        self.count(3)
        self.portal.login.assert_called_once()

    def test_login_failure_retries_only_after_another_increase(self):
        self.count(0)
        self.portal.login.side_effect = app.Problem("测试失败")
        with self.assertRaises(app.Problem):
            self.count(1)
        self.count(1)
        self.portal.login.assert_called_once()
        with self.assertRaises(app.Problem):
            self.count(3)
        self.assertEqual(self.portal.login.call_count, 2)

    def test_first_sample_does_not_trigger_action(self):
        for count in (3, 0):
            engine = app.Engine(self.portal, self.portal.login.return_value, self.save)
            engine.step(count)
        self.assertEqual(self.portal.mock_calls, [])

    def test_unknown_does_not_change_count_baseline(self):
        self.count(0)
        self.count(2)
        self.portal.reset_mock()
        self.engine.step(None)
        self.count(1)
        self.assertEqual(self.portal.mock_calls, [])

    def test_manual_login_handles_stale_session_without_rearming(self):
        self.count(0)
        self.count(1)
        self.portal.online.return_value = False
        self.assertTrue(app.execute_manual(self.engine, "login")["ok"])
        self.assertEqual(self.portal.login.call_count, 2)
        self.portal.reset_mock()
        self.count(3)
        self.portal.login.assert_called_once()

    def test_manual_login_does_not_duplicate_valid_login(self):
        self.count(0)
        self.count(1)
        self.assertTrue(app.execute_manual(self.engine, "login")["ok"])
        self.portal.login.assert_called_once()

    def test_manual_login_failure_keeps_baseline(self):
        self.count(0)
        self.portal.login.side_effect = app.Problem("测试失败")
        self.assertFalse(app.execute_manual(self.engine, "login")["ok"])
        self.count(0)
        self.portal.login.assert_called_once()


class ManualCommandTests(unittest.TestCase):
    def test_running_service_receives_manual_commands(self):
        for action in ("login", "logout", "status"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder, patch.object(
                app, "BASE", Path(folder) / "config"
            ), patch.object(app, "RUNTIME", Path(folder) / "runtime"), patch.object(
                app.sys, "argv", ["campus_wifi.py", action]
            ), patch.object(app.fcntl, "flock", side_effect=BlockingIOError), patch.object(
                app, "request_command"
            ) as forward:
                app.main()
                forward.assert_called_once_with(action)

    def test_status_result_contains_wifi_snapshot(self):
        snapshot = {
            "presence": "present",
            "association_records": 1,
            "records": [{"interface": "rai2", "mac": "00:11:22:33:44:55"}],
            "aps": ["rai2"],
            "errors": [],
        }
        with patch.object(app, "wifi_snapshot", return_value=snapshot):
            result = app.execute_status()
        self.assertTrue(result["ok"])
        self.assertIn("无线状态：有设备连接", result["message"])
        self.assertIn("关联记录数：1", result["message"])

    def test_manual_logout_preserves_wifi_baseline(self):
        portal = Mock()
        state = {"userIndex": "test", "username": "dummy"}
        engine = app.Engine(portal, state, Mock())
        engine.previous_count = 1
        result = app.execute_manual(engine, "logout")
        self.assertTrue(result["ok"])
        self.assertIsNone(engine.state)
        engine.step(1)
        portal.login.assert_not_called()
        portal.logout.assert_called_once_with(state)

    def test_failed_manual_logout_preserves_session(self):
        portal = Mock()
        portal.logout.side_effect = app.Problem("登出失败")
        state = {"userIndex": "test", "username": "dummy"}
        engine = app.Engine(portal, state, Mock())
        result = app.execute_manual(engine, "logout")
        self.assertFalse(result["ok"])
        self.assertEqual(engine.state, state)

    def test_direct_login_checks_existing_session(self):
        portal = Mock()
        portal.online.return_value = True
        engine = app.Engine(portal, {"userIndex": "test"}, Mock())
        self.assertTrue(app.execute_manual(engine, "login")["ok"])
        portal.login.assert_not_called()

    def test_control_roundtrip_returns_actual_result(self):
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory(
                dir="/tmp"
            ) as folder, patch.object(app, "RUNTIME", Path(folder)), socket.socket(
                socket.AF_UNIX, socket.SOCK_DGRAM
            ) as server:
                server.bind(str(Path(folder) / "control.sock"))
                server.settimeout(2)

                def respond():
                    raw, address = server.recvfrom(4096)
                    request = json.loads(raw)
                    result = {
                        "id": request["id"],
                        "ok": success,
                        "message": "登出成功" if success else "登出失败",
                    }
                    server.sendto(json.dumps(result).encode(), address)

                thread = threading.Thread(target=respond)
                thread.start()
                try:
                    if success:
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            app.request_command("logout")
                        self.assertIn("登出成功", output.getvalue())
                    else:
                        with self.assertRaisesRegex(app.Problem, "登出失败"):
                            app.request_command("logout")
                finally:
                    thread.join(timeout=3)
                self.assertFalse(list(Path(folder).glob("reply-*.sock")))


class PortalTests(unittest.TestCase):
    def test_no_login_without_fresh_redirect(self):
        portal = app.Portal({"username": "test", "password": "dummy"})
        with patch.object(portal, "discover", return_value=None), patch.object(
            portal, "api"
        ) as api:
            with self.assertRaises(app.Problem):
                portal.login()
            api.assert_not_called()

    def test_login_uses_fresh_mac_and_double_encoding(self):
        portal = app.Portal({"username": "test", "password": "dummy"})
        url = "http://10.255.100.2/eportal/index.jsp?wlanuserip=new&mac=fresh"
        with patch.object(portal, "discover", return_value=url), patch.object(
            portal, "request"
        ), patch.object(
            portal,
            "api",
            side_effect=[
                {"publicKeyExponent": "10001", "publicKeyModulus": "ab"},
                {"result": "success", "userIndex": "test"},
            ],
        ) as api, patch.object(app, "rsa_password", return_value="encrypted") as rsa:
            result = portal.login()
            self.assertEqual(result["userIndex"], "test")
            self.assertEqual(result["portal_url"], app.DEFAULT_PORTAL_URL)
            rsa.assert_called_once_with("dummy", "fresh", "10001", "ab")
            self.assertIn("queryString=wlanuserip%253Dnew%2526mac%253Dfresh", api.call_args[0][1])

    def test_api_uses_configured_portal_url(self):
        portal = app.Portal(
            {
                "username": "test",
                "password": "dummy",
                "portal_url": "https://portal.example.edu:8443",
            }
        )
        with patch.object(portal, "request", return_value='{"result":"success"}') as request:
            portal.api("getOnlineUserInfo", "userIndex=test")
        self.assertEqual(
            request.call_args.args[0],
            "https://portal.example.edu:8443/eportal/InterFace.do?method=getOnlineUserInfo",
        )

    def test_request_origin_uses_configured_portal_url(self):
        portal = app.Portal(
            {
                "username": "test",
                "password": "dummy",
                "portal_url": "https://portal.example.edu:8443",
            }
        )
        completed = Mock(returncode=0, stdout="", stderr="")
        with patch.object(app.subprocess, "run", return_value=completed) as run:
            portal.request("https://portal.example.edu:8443/test", data="x=1")
        self.assertIn(
            "Origin: https://portal.example.edu:8443",
            run.call_args.args[0],
        )

    def test_help_and_errors_are_chinese(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
            app.parse_action(["--help"])
        self.assertIn("可用命令", output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(
            app.Problem, "有效命令"
        ):
            app.parse_action(["invalid"])



if __name__ == "__main__":
    unittest.main()
