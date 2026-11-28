"""scripts/check.sh refuses to start, with exit 2, when any tool it needs is missing.

Each case puts stubs for every tool on PATH but one. A stub records that it ran,
so the case also shows no check started before the refusal.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check.sh"
TOOLS = ["cargo", "bend", "bun", "node", "java", "uv", "shellcheck", "cargo-deny"]


class MissingTool(unittest.TestCase):
    def run_without(self, missing):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            ran = Path(tmp) / "ran"
            for name in ("bash", "dirname"):
                found = shutil.which(name)
                self.assertIsNotNone(found, name)
                (bin_dir / name).symlink_to(found)
            for tool in TOOLS:
                if tool == missing:
                    continue
                stub = bin_dir / tool
                stub.write_text(f'#!{shutil.which("bash")}\necho {tool} >> "{ran}"\n')
                stub.chmod(0o755)
            jar = Path(tmp) / "tla2tools.jar"
            jar.write_text("")
            env = {"PATH": str(bin_dir), "TLA2TOOLS": str(jar)}
            proc = subprocess.run(
                [str(bin_dir / "bash"), str(CHECK)], env=env, capture_output=True, text=True
            )
            return proc, ran.read_text() if ran.exists() else ""

    def test_each_missing_tool_stops_the_check(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                proc, ran = self.run_without(tool)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(f"check: {tool} is not installed", proc.stderr)
                self.assertEqual(ran, "", "a tool ran before the refusal")


if __name__ == "__main__":
    unittest.main()
