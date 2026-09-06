"""Regression tests for the opt-in startup hook; no GPU or downloads needed."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dlss5-support"))
import support


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.node = self.root / "custom node with spaces"
        self.state = self.root / "persistent state"
        self.env = {"DLSS5_AUTO_SETUP": "true", "DLSS5_NODE_DIR": str(self.node),
                    "DLSS5_STATE_DIR": str(self.state)}

    def make_node(self, runtime=True):
        (self.node / "native").mkdir(parents=True)
        (self.node / "native/build_linux.sh").touch()
        (self.node / "install_runtime.py").touch()
        if runtime:
            (self.node / "runtime").mkdir()
            for name in support.NODE_FILES[:2]:
                (self.node / "runtime" / name).write_bytes(b"existing")

    def test_disabled_by_default_and_no_side_effects(self):
        for env in ({}, {"DLSS5_AUTO_SETUP": "FALSE"}):
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(support, "prepare") as prepare, \
                 patch.object(support, "run") as run, \
                 patch.object(support, "private_dir") as state:
                support.startup()
                prepare.assert_not_called()
                run.assert_not_called()
                state.assert_not_called()

    def test_invalid_boolean_fails_before_setup(self):
        with patch.dict(os.environ, {"DLSS5_AUTO_SETUP": "tru"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "true or false"):
                support.startup()

    def test_missing_node_skips_and_uses_default_base_or_comfy_path(self):
        for key in ("BASE_DIRECTORY", "COMFYUI_PATH"):
            with patch.dict(os.environ, {"DLSS5_AUTO_SETUP": "true", key: str(self.root)}, clear=True), \
                 patch.object(support, "prepare") as prepare, \
                 contextlib.redirect_stdout(io.StringIO()) as log:
                support.startup()
                self.assertIn(str(self.root / "custom_nodes/ComfyUI-DLSS5-Enhancer"), log.getvalue())
                self.assertIn("skipping", log.getvalue())
                prepare.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_existing_runtime_never_runs_installer(self):
        self.make_node()
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(support, "prepare") as prepare, \
             patch.object(support, "run") as run:
            support.startup()
            run.assert_not_called()
            args = prepare.call_args.args[0]
            self.assertEqual(args.node_dir, self.node)
            self.assertEqual(args.state_dir, self.state)
            self.assertEqual(args.runtime_dir, self.node / "runtime")
            self.assertTrue(args.download)

    def test_first_install_uses_venv_and_preserves_config(self):
        self.make_node(runtime=False)
        old = {"custom": {"keep": True}, "wine_prefix": "/old/prefix"}
        config = self.node / "config.json"
        config.write_text(json.dumps(old))
        python = self.root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        self.env["VIRTUAL_ENV"] = str(python.parent.parent)

        def fake_installer(command, **kwargs):
            self.assertEqual(command, [str(python), str(self.node / "install_runtime.py"), "--yes"])
            config.write_text(json.dumps({"runtime_dir": str(self.node / "runtime")}))
            (self.node / "runtime").mkdir()
            for name in support.NODE_FILES[:2]:
                (self.node / "runtime" / name).write_bytes(b"installed")

        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(support, "run", side_effect=fake_installer) as run, \
             patch.object(support, "prepare") as prepare:
            support.startup()
            self.assertEqual(support.read_config(config)["custom"], old["custom"])
            backups = list(self.node.glob("config.json.before-dlss5-*"))
            self.assertTrue(any(support.read_config(path) == old for path in backups))
            support.startup()
            run.assert_called_once()
            self.assertEqual(prepare.call_count, 2)

    def test_failed_installer_does_not_prepare(self):
        self.make_node(runtime=False)
        python = self.root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        self.env["VIRTUAL_ENV"] = str(python.parent.parent)
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(support, "run", side_effect=subprocess.CalledProcessError(1, "installer")), \
             patch.object(support, "prepare") as prepare:
            with self.assertRaises(subprocess.CalledProcessError):
                support.startup()
            prepare.assert_not_called()

    def test_incomplete_external_runtime_is_not_replaced(self):
        self.make_node(runtime=False)
        config = self.node / "config.json"
        old = {"runtime_dir": str(self.root / "unmounted-runtime")}
        config.write_text(json.dumps(old))
        with patch.dict(os.environ, self.env, clear=True), patch.object(support, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Configured runtime"):
                support.startup()
            run.assert_not_called()
            self.assertEqual(support.read_config(config), old)

    def test_conflicting_runtime_environment_is_rejected(self):
        self.make_node()
        self.env["DLSS5_RUNTIME_DIR"] = str(self.node / "runtime")
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Unset DLSS5_RUNTIME_DIR"):
                support.startup()

    def test_entrypoint_hook_is_opt_in_and_failure_is_nonfatal(self):
        init = (ROOT / "init.bash").read_text()
        begin = init.index("# BEGIN optional DLSS auto-setup")
        end = init.index("# END optional DLSS auto-setup")
        self.assertGreater(begin, init.index('run_userscript $it "chmod"', init.index("# Check for the main custom user script")))
        self.assertLess(end, init.index("${COMFY_CMDLINE_BASE} ${COMFY_CMDLINE_EXTRA} ||"))
        hook = init[begin:end]
        helper = self.root / "comfy-dlss5-setup"
        helper.write_text('#!/bin/bash\nprintf "helper:%s:%s\\n" "$DLSS5_AUTO_SETUP" "$*"\nexit 1\n')
        helper.chmod(0o755)
        for enabled in (None, "false", "FALSE", "true"):
            env = {"PATH": f"{self.root}:/usr/bin:/bin"}
            if enabled is not None:
                env["DLSS5_AUTO_SETUP"] = enabled
            result = subprocess.run(["bash", "-ec", hook + '\nprintf "ComfyUI continues\\n"\n'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ComfyUI continues", result.stdout)
            if enabled == "true":
                self.assertIn("helper:true:--startup", result.stdout)
                self.assertIn("DLSS auto-setup failed", result.stdout)
            else:
                self.assertNotIn("helper:", result.stdout)


if __name__ == "__main__":
    unittest.main()
