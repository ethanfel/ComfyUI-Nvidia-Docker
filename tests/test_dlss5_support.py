"""CPU-only safety/regression tests: python3 -B -m unittest discover -s tests."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dlss5-support"))
import support


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_download_requires_explicit_opt_in(self):
        with patch("urllib.request.urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "opt-in"):
                support.download("https://example.invalid/archive", self.root / "archive", False)
            network.assert_not_called()

    def test_checksum_failure_never_promotes_download(self):
        target = self.root / "archive"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(RuntimeError, "Checksum mismatch"):
                support.download("https://example.invalid/archive", target, True, "bad-hash")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_extracts_only_expected_regular_graphics_files(self):
        archive = self.root / "proton.tar.xz"
        with tarfile.open(archive, "w:xz") as tar:
            for name, group in support.GRAPHICS.items():
                data = name.encode()
                member = tarfile.TarInfo(f"proton/files/lib/wine/{group}/x86_64-windows/{name}")
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            member = tarfile.TarInfo("../../must-not-extract")
            member.size = 3
            tar.addfile(member, io.BytesIO(b"bad"))
        args = argparse.Namespace(proton_dir=None, download=False)
        with patch.object(support, "download", return_value=archive):
            files = support.graphics_files(args, self.root)
        self.assertEqual(set(files), set(support.GRAPHICS))
        for name, path in files.items():
            self.assertEqual(path.read_bytes(), name.encode())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"proton.tar.xz", "graphics-20260521"})

    def test_ngx_uses_loader_path_and_rejects_driver_mismatch(self):
        driver_dir = self.root / "extracted-driver"
        driver_dir.mkdir()
        for name in ("nvngx.dll", "_nvngx.dll", "libnvidia-ngx.so.595.84"):
            (driver_dir / name).write_bytes(b"matching")
        injected = self.root / "libnvidia-ngx.so.1"
        injected.write_bytes(b"matching")
        args = argparse.Namespace(ngx_dir=driver_dir)
        with patch.object(support, "output", return_value=f"libnvidia-ngx.so.1 (libc6,x86-64) => {injected}"):
            files = support.ngx_files(args, self.root, "595.84")
            self.assertEqual(set(files), {"nvngx.dll", "_nvngx.dll"})
            injected.write_bytes(b"wrong version")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                support.ngx_files(args, self.root, "595.84")

    def test_config_merge_backup_and_idempotence(self):
        config = self.root / "config.json"
        old = {"unrelated": {"keep": True}, "wine_prefix": "/old/prefix"}
        config.write_text(json.dumps(old))
        support.write_config(config, old, {"wine_prefix": "/new/prefix"})
        self.assertEqual(support.read_config(config)["unrelated"], old["unrelated"])
        backups = list(self.root.glob("*.before-dlss5-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(support.read_config(backups[0]), old)
        support.write_config(config, support.read_config(config), {"wine_prefix": "/new/prefix"})
        self.assertEqual(list(self.root.glob("*.before-dlss5-*")), backups)

    def test_reject_nonobject_config(self):
        config = self.root / "config.json"
        config.write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "JSON object"):
            support.read_config(config)
        self.assertEqual(config.read_text(), "[]")

    def test_driver_version_validation(self):
        for invalid in ("", "595.84\n610.43", "../../etc", "595.84;sh"):
            with self.subTest(invalid=invalid), patch.object(support, "output", return_value=invalid):
                with self.assertRaises(RuntimeError):
                    support.driver_version()
        with patch.object(support, "output", return_value="595.84\n595.84"):
            self.assertEqual(support.driver_version(), "595.84")

    def test_private_directory_rejects_symlink_or_shared_mode(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            support.private_dir(link)
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        with self.assertRaises(RuntimeError):
            support.private_dir(shared)

    def test_bad_display_rejected_before_starting_processes(self):
        with patch.dict(os.environ, {"DLSS5_DISPLAY": "remote:0"}), patch.object(subprocess, "Popen") as proc:
            with self.assertRaises(RuntimeError):
                support.display_env()
            proc.assert_not_called()

    def test_display_selection_skips_stale_locks_and_sockets(self):
        occupied = {"/tmp/.X98-lock", "/tmp/.X11-unix/X99"}
        with patch.object(os.path, "lexists", side_effect=lambda path: path in occupied):
            self.assertEqual(support.free_display(), ":100")

    def test_display_exhaustion_fails_without_removing_locks(self):
        with patch.object(os.path, "lexists", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "No free DLSS display"):
                support.free_display()

    def test_live_saved_display_is_reused_without_starting_xorg(self):
        (self.root / "display.txt").write_text(":101\n")
        reply = subprocess.CompletedProcess(["xrandr"], 0, stdout="1920x1080 60.00*+", stderr="")
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(support, "private_dir", return_value=self.root), \
             patch.object(subprocess, "run", return_value=reply), \
             patch.object(subprocess, "Popen") as server:
            env = support.display_env()
            self.assertEqual(env["DISPLAY"], ":101")
            server.assert_not_called()

    def test_stale_driver_blocks_launch_without_download_or_display(self):
        prefix = self.root / "wineprefix"
        prefix.mkdir()
        (prefix / "system.reg").touch()
        (self.root / "manifest.json").write_text(json.dumps({"driver": "595.84", "wine": "wine-11.17"}))
        with patch.dict(os.environ, {"WINEPREFIX": str(prefix)}), \
             patch.object(support, "driver_version", return_value="610.43"), \
             patch.object(support, "display_env") as display, \
             patch.object(support, "download") as download:
            with self.assertRaisesRegex(RuntimeError, "driver changed"):
                support.launch_wine(["worker.exe"])
            display.assert_not_called()
            download.assert_not_called()

    def test_launch_executes_wine_without_interposing_on_stdout(self):
        prefix = self.root / "wineprefix"
        prefix.mkdir()
        (prefix / "system.reg").touch()
        (self.root / "manifest.json").write_text(json.dumps({"driver": "595.84", "wine": "wine-11.17"}))
        env = {"WINEPREFIX": str(prefix), "DISPLAY": ":98"}
        with patch.dict(os.environ, env), \
             patch.object(support, "driver_version", return_value="595.84"), \
             patch.object(support, "output", return_value="wine-11.17"), \
             patch.object(support, "display_env", return_value=env), \
             patch.object(os, "execve") as execute:
            support.launch_wine(["a worker.exe", "Z:\\runtime"])
            execute.assert_called_once_with(support.WINE, [support.WINE, "a worker.exe", "Z:\\runtime"], env)

    def test_setup_preserves_old_prefix_and_reuses_managed_bundle(self):
        node = self.root / "node"
        (node / "native").mkdir(parents=True)
        (node / "native/build_linux.sh").touch()
        runtime = node / "runtime"
        for name in support.NODE_FILES:
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(name.encode())
        old_prefix = self.root / "old-prefix"
        old_prefix.mkdir()
        (old_prefix / "sentinel").write_text("keep")
        (node / "config.json").write_text(json.dumps({"wine_prefix": str(old_prefix), "custom": True}))
        dlls = {}
        for name in (*support.GRAPHICS, "nvngx.dll", "_nvngx.dll"):
            dlls[name] = self.root / name
            dlls[name].write_bytes(name.encode())
        args = argparse.Namespace(node_dir=node, runtime_dir=None, state_dir=self.root / "state")

        def fake_boot(command, **kwargs):
            if command[0] == "/usr/bin/wineboot":
                prefix = Path(kwargs["env"]["WINEPREFIX"])
                (prefix / "drive_c/windows/system32").mkdir(parents=True)
                (prefix / "system.reg").touch()
            return subprocess.CompletedProcess(command, 0)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch.object(support, "driver_version", return_value="595.84"))
            stack.enter_context(patch.object(support, "output", return_value="wine-11.17"))
            stack.enter_context(patch.object(support, "graphics_files", return_value={n: dlls[n] for n in support.GRAPHICS}))
            stack.enter_context(patch.object(support, "ngx_files", return_value={n: dlls[n] for n in ("nvngx.dll", "_nvngx.dll")}))
            display = stack.enter_context(patch.object(support, "display_env", return_value={}))
            stack.enter_context(patch.object(subprocess, "run", side_effect=fake_boot))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            support.prepare(args)
            first = support.read_config(node / "config.json")
            support.prepare(args)
            self.assertEqual(first, support.read_config(node / "config.json"))
            self.assertTrue(first["custom"])
            display.assert_called_once()
            self.assertEqual((old_prefix / "sentinel").read_text(), "keep")
            self.assertEqual(len(list((args.state_dir / "bundles").iterdir())), 1)
            changed = Path(first["wine_prefix"]) / "drive_c/windows/system32/dxgi.dll"
            changed.write_text("tampered")
            with self.assertRaisesRegex(RuntimeError, "prefix DLL changed"):
                support.prepare(args)
            self.assertEqual(changed.read_text(), "tampered")


if __name__ == "__main__":
    unittest.main()
