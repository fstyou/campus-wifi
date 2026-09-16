"""验证按键绑定不改变原有分支，并能完整恢复原文件。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bind_reset as binding

ORIGINAL = b"""#!/bin/sh
case "$ACTION" in
pressed)
    reset_btn_pressed
;;
released)
\treset_btn_released
    if [ "$SEEN" -ge 20 ]; then
        return 0
    elif [ "$SEEN" -ge 8 ]; then
        factory_reset
    elif [ "$SEEN" -ge 3 ]; then
        reset_network &
    fi
;;
esac
return 0
"""


class BindingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = Path(directory.name) / "reset"
        self.backup = Path(directory.name) / "backup.json"
        self.target.write_bytes(ORIGINAL)
        self.target.chmod(0o775)

    def test_preserves_original_except_single_hook(self):
        binding.bind("enable", self.target, self.backup)
        patched = self.target.read_text()
        self.assertEqual(patched.replace(binding.HOOK, ""), ORIGINAL.decode())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o775)

    def test_enable_is_idempotent(self):
        binding.bind("enable", self.target, self.backup)
        first = self.target.read_bytes()
        binding.bind("enable", self.target, self.backup)
        self.assertEqual(self.target.read_bytes(), first)

    def test_restores_exact_original(self):
        binding.bind("enable", self.target, self.backup)
        binding.bind("disable", self.target, self.backup)
        self.assertEqual(self.target.read_bytes(), ORIGINAL)
        binding.bind("enable", self.target, self.backup)
        self.assertIn("已绑定", binding.bind("status", self.target, self.backup))

    def test_refuses_to_overwrite_external_changes(self):
        binding.bind("enable", self.target, self.backup)
        modified = self.target.read_bytes() + b"\n# changed\n"
        self.target.write_bytes(modified)
        for action in ["enable", "disable"]:
            with self.assertRaises(binding.BindError):
                binding.bind(action, self.target, self.backup)
        self.assertEqual(self.target.read_bytes(), modified)

    def test_refuses_unknown_firmware(self):
        self.target.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaises(binding.BindError):
            binding.bind("enable", self.target, self.backup)
        self.assertFalse(self.backup.exists())


if __name__ == "__main__":
    unittest.main()
