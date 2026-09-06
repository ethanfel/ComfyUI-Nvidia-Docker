"""Container-side DLSS support; deliberately independent of the ComfyUI venv."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

WINE = "/usr/bin/wine"
WRAPPER = "/usr/local/bin/comfy-dlss5-wine"
PROTON_URL = (
    "https://github.com/CachyOS/proton-cachyos/releases/download/"
    "cachyos-11.0-20260521-slr/proton-cachyos-11.0-20260521-slr-x86_64.tar.xz"
)
PROTON_SHA256 = "b2f0ec8e931b02cbcb575c6f4cf1f37932a648a3add271dd7dd12f1395f5b72c"
GRAPHICS = {
    "d3d12.dll": "vkd3d-proton",
    "d3d12core.dll": "vkd3d-proton",
    "dxgi.dll": "dxvk",
    "nvapi64.dll": "nvapi",
}
NODE_FILES = ("nvngx_dlss.dll", "nvngx_dlssnr.dll",
              "linux/dlss5-worker.exe", "caller/nvngx.dll_comfy.dll")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args, **kwargs):
    # Never send diagnostic output into the worker's binary stdout protocol.
    return subprocess.run(args, check=True, stdout=sys.stderr, **kwargs)


def output(args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def driver_version():
    versions = set(output(["nvidia-smi", "--query-gpu=driver_version",
                           "--format=csv,noheader"]).splitlines())
    if len(versions) != 1 or not re.fullmatch(r"\d{3}\.\d{1,3}(?:\.\d{1,3})?", next(iter(versions), "")):
        raise RuntimeError("Cannot identify one NVIDIA host driver version.")
    return versions.pop()


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"Expected a private directory owned by UID {os.getuid()}: {path}")
    return path


@contextlib.contextmanager
def lock(path):
    # Lock lives in a private directory; do not follow a substituted symlink.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def free_display():
    # Container stop can leave an X lock whose PID is reused by a new process.
    # Avoid occupied/stale slots instead of deleting locks or killing anything.
    for number in range(98, 4096):
        if not any(os.path.lexists(path) for path in
                   (f"/tmp/.X{number}-lock", f"/tmp/.X11-unix/X{number}")):
            return f":{number}"
    raise RuntimeError("No free DLSS display between :98 and :4095.")


def display_env():
    """One authenticated, TCP-disabled dummy X server per container UID."""
    requested = os.environ.get("DLSS5_DISPLAY")
    if requested is not None and not re.fullmatch(r":[1-9]\d{0,3}", requested):
        raise RuntimeError("DLSS5_DISPLAY must be a local display such as :98.")
    state = private_dir(Path(f"/tmp/comfy-dlss5-{os.getuid()}"))
    env = dict(os.environ, XDG_RUNTIME_DIR=str(state))

    def select(display):
        env.update(DISPLAY=display, XAUTHORITY=str(state / f"Xauthority-{display[1:]}"))
    if not env.get("VK_DRIVER_FILES") and not env.get("VK_ICD_FILENAMES"):
        for root in ("/etc", "/usr/share"):
            icd = Path(root) / "vulkan/icd.d/nvidia_icd.json"
            if icd.is_file():
                env.update(VK_DRIVER_FILES=str(icd), VK_ICD_FILENAMES=str(icd))
                break

    def ready():
        try:
            result = subprocess.run(["xrandr", "--current"], env=env,
                                    capture_output=True, text=True, timeout=3)
            return result.returncode == 0 and bool(re.search(r"60\.0+\*", result.stdout))
        except subprocess.TimeoutExpired:
            return False

    with lock(state / "display.lock"):
        saved = state / "display.txt"
        display = requested or (saved.read_text().strip() if saved.is_file() else ":98")
        if not re.fullmatch(r":[1-9]\d{0,3}", display):
            display = ":98"
        select(display)
        if ready():
            return env
        if requested is None:
            display = free_display()
            select(display)
        auth = Path(env["XAUTHORITY"])
        auth.touch(mode=0o600, exist_ok=True)
        run(["xauth", "-f", str(auth), "add", display, "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)])
        log = state / f"xorg-{display[1:]}.log"
        with (state / f"xorg-{display[1:]}.stderr").open("ab") as err:
            server = subprocess.Popen([
                "/usr/lib/xorg/Xorg", display,
                "-config", str(Path(__file__).with_name("xorg-dummy.conf")),
                "-logfile", str(log), "-auth", str(auth),
                "-nolisten", "tcp", "-noreset",
            ], env=env, stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                close_fds=True, start_new_session=True)
        for _ in range(50):
            if server.poll() is not None:
                break
            if ready():
                saved.write_text(display + "\n")
                return env
            time.sleep(0.1)
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        raise RuntimeError(f"Headless display failed. Inspect {log} and {log.with_suffix('.stderr')}. If the display is occupied, set another DLSS5_DISPLAY (e.g. :99).")


def download(url, destination, allowed, checksum=None):
    if destination.is_file() and (not checksum or digest(destination) == checksum):
        return destination
    if not allowed:
        raise RuntimeError("Downloads are opt-in. Use --download or supply --proton-dir and --ngx-dir.")
    print(f"Downloading {url}", file=sys.stderr)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix="download-")
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=60) as src:
            shutil.copyfileobj(src, out)
        if checksum and digest(tmp) != checksum:
            raise RuntimeError(f"Checksum mismatch for {url}; refusing to use it.")
        os.replace(tmp, destination)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return destination


def graphics_files(args, cache):
    if args.proton_dir:
        root = args.proton_dir.resolve() / "files/lib/wine"
        files = {name: root / group / "x86_64-windows" / name for name, group in GRAPHICS.items()}
    else:
        archive = download(PROTON_URL, cache / "proton-cachyos-11.0-20260521.tar.xz",
                           args.download, PROTON_SHA256)
        target = cache / "graphics-20260521"
        target.mkdir(exist_ok=True)
        files = {}
        with tarfile.open(archive, "r:xz") as tar:
            members = tar.getmembers()
            for name, group in GRAPHICS.items():
                suffix = f"/files/lib/wine/{group}/x86_64-windows/{name}"
                matches = [m for m in members if ("/" + m.name).endswith(suffix) and m.isfile()]
                if len(matches) != 1:
                    raise RuntimeError(f"Expected exactly one regular {name} in Proton archive.")
                # Extract only four regular files to fixed paths, never archive paths.
                with tar.extractfile(matches[0]) as src, (target / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                files[name] = target / name
    for path in files.values():
        if not path.is_file():
            raise RuntimeError(f"Missing graphics DLL: {path}")
    return files


def ngx_files(args, cache, driver):
    if args.ngx_dir:
        root = args.ngx_dir.resolve()
    else:
        name = f"NVIDIA-Linux-x86_64-{driver}"
        root = cache / name
        if not root.is_dir():
            archive = download(f"https://download.nvidia.com/XFree86/Linux-x86_64/{driver}/{name}.run",
                               cache / f"{name}.run", args.download)
            # --check verifies the self-extractor; --extract-only never installs a driver.
            run(["sh", str(archive), "--check"], timeout=180)
            run(["sh", str(archive), "--extract-only"], cwd=cache, timeout=180)
    native = root / f"libnvidia-ngx.so.{driver}"
    injected = next((Path(p) for p in re.findall(
        r"libnvidia-ngx\.so\.1 \(libc6,x86-64\) => (\S+)", output(["/sbin/ldconfig", "-p"]))
        if Path(p).is_file()), None)
    if injected is None:
        raise RuntimeError("Missing host-injected libnvidia-ngx.so.1. Enable NVIDIA_DRIVER_CAPABILITIES=all and recreate the container.")
    if not native.is_file() or digest(native) != digest(injected):
        raise RuntimeError("NGX package does not match the host-injected Linux library. Supply the exact host driver package.")
    files = {name: root / name for name in ("nvngx.dll", "_nvngx.dll")}
    for path in files.values():
        if not path.is_file():
            raise RuntimeError(f"Missing driver DLL: {path}")
    return files


def read_config(path):
    config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict):
        raise RuntimeError(f"Expected a JSON object in {path}; refusing to replace it.")
    return config


def backup_config(path):
    if path.exists():
        backup = path.with_name(f"{path.name}.before-dlss5-{time.time_ns()}")
        shutil.copy2(path, backup)
        print(f"Previous config saved: {backup}", file=sys.stderr)


def write_config(path, old, updates):
    config = {**old, **updates}
    if config == old:
        return
    backup_config(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dlss5-config-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def prepare(args):
    node = args.node_dir.resolve()
    if not (node / "native/build_linux.sh").is_file():
        raise RuntimeError(f"Install the Linux-capable enhancer first: {node}")
    config_path = node / "config.json"
    config = read_config(config_path)
    source = Path(args.runtime_dir or os.environ.get("DLSS5_RUNTIME_DIR") or
                  config.get("runtime_dir", node / "runtime")).resolve()
    for name in NODE_FILES[:2]:
        if not (source / name).is_file():
            raise RuntimeError(f"Missing {source / name}. Run the enhancer's runtime installer first.")
    driver = driver_version()
    wine = output([WINE, "--version"])
    state = private_dir(args.state_dir.resolve())
    with lock(state / "setup.lock"):
        cache = state / "cache"
        cache.mkdir(exist_ok=True)
        graphics = graphics_files(args, cache)
        ngx = ngx_files(args, cache, driver)
        if not all((source / name).is_file() for name in NODE_FILES[2:]):
            run(["bash", str(node / "native/build_linux.sh")], cwd=node, timeout=180)
            worker_root = node / "runtime"
        else:
            worker_root = source
        files = {name: source / name for name in NODE_FILES[:2]}
        files.update({name: worker_root / name for name in NODE_FILES[2:]})
        manifest = {"schema": 1, "wine": wine, "driver": driver,
                    "graphics": {name: digest(path) for name, path in graphics.items()},
                    "ngx": {name: digest(path) for name, path in ngx.items()},
                    "runtime": {name: digest(path) for name, path in files.items()}}
        key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
        bundle = state / "bundles" / f"driver-{driver}-{key}"
        prefix = bundle / "wineprefix"
        runtime = bundle / "runtime"
        marker = bundle / "manifest.json"
        if bundle.exists() and not marker.is_file():
            raise RuntimeError(f"Incomplete previous setup retained at {bundle}. Inspect its wineboot.log; use another --state-dir to retry without deleting it.")
        if not bundle.exists():
            env = display_env()
            bundle.mkdir(parents=True)
            env.update(WINEPREFIX=str(prefix), WINEARCH="win64", WINEDEBUG="-all",
                       WINEDLLOVERRIDES="winemenubuilder,mscoree,mshtml=d")
            with (bundle / "wineboot.log").open("wb") as log:
                try:
                    subprocess.run(["/usr/bin/wineboot", "-u"], env=env, stdout=log,
                                   stderr=log, check=True, timeout=90)
                finally:
                    # Only this newly-created prefix, never an existing user prefix.
                    subprocess.run(["/usr/bin/wineserver", "-k"], env=env, stdout=log, stderr=log, timeout=15)
                    subprocess.run(["/usr/bin/wineserver", "-w"], env=env, stdout=log, stderr=log, timeout=20)
            if not (prefix / "system.reg").is_file():
                raise RuntimeError(f"Wine prefix creation failed; see {bundle / 'wineboot.log'}")
            for name, src in {**graphics, **ngx}.items():
                dst = prefix / "drive_c/windows/system32" / name
                # Wine may install symlinks: replace the link, not its global target.
                dst.unlink(missing_ok=True)
                shutil.copy2(src, dst)
            for name, src in {**files, "_nvngx.dll": ngx["_nvngx.dll"]}.items():
                dst = runtime / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            marker.write_text(json.dumps(manifest, indent=2) + "\n")
        else:
            if read_config(marker) != manifest or not (prefix / "system.reg").is_file():
                raise RuntimeError(f"Existing managed bundle is inconsistent: {bundle}")
            for name, expected in {**manifest["runtime"], "_nvngx.dll": manifest["ngx"]["_nvngx.dll"]}.items():
                if not (runtime / name).is_file() or digest(runtime / name) != expected:
                    raise RuntimeError(f"Managed runtime changed: {runtime / name}. Nothing overwritten.")
            for name, expected in {**manifest["graphics"], **manifest["ngx"]}.items():
                installed = prefix / "drive_c/windows/system32" / name
                if not installed.is_file() or digest(installed) != expected:
                    raise RuntimeError(f"Managed prefix DLL changed: {installed}. Nothing overwritten.")
        write_config(config_path, config, {"wine_executable": WRAPPER,
                     "wine_prefix": str(prefix), "runtime_dir": str(runtime), "ffmpeg_dir": "/usr/bin"})
    print(f"DLSS ready. Persistent prefix: {prefix}\nRuntime: {runtime}\nNo downloads or setup commands are needed per render.")
    if os.environ.get("DLSS5_RUNTIME_DIR"):
        print(f"IMPORTANT: unset DLSS5_RUNTIME_DIR in ComfyUI or set it to {runtime}; it overrides config.json.", file=sys.stderr)


def launch_wine(args):
    prefix = Path(os.environ.get("WINEPREFIX", ""))
    if not prefix.is_absolute() or not (prefix / "system.reg").is_file():
        raise RuntimeError("An initialized WINEPREFIX is required. Run comfy-dlss5-setup first.")
    manifest = read_config(prefix.parent / "manifest.json")
    if not manifest or manifest.get("driver") != driver_version() or manifest.get("wine") != output([WINE, "--version"]):
        raise RuntimeError("Wine/host driver changed, or this prefix is unmanaged. Rerun comfy-dlss5-setup; old prefixes are preserved.")
    env = display_env()
    env.setdefault("WINEDEBUG", "-all")
    # exec preserves the worker protocol and the node's process-group cancellation.
    os.execve(WINE, [WINE, *args], env)


def startup():
    """Opt-in startup setup, after the venv and user scripts are ready."""
    enabled = os.environ.get("DLSS5_AUTO_SETUP", "false").lower()
    if enabled == "false":
        return
    if enabled != "true":
        raise RuntimeError("DLSS5_AUTO_SETUP must be true or false.")
    base = os.environ.get("BASE_DIRECTORY")
    if not base or base == "VALUE_TO_IGNORE":
        base = os.environ.get("COMFYUI_PATH", "/comfy/mnt/ComfyUI")
    node = Path(os.environ.get("DLSS5_NODE_DIR") or
                str(Path(base) / "custom_nodes/ComfyUI-DLSS5-Enhancer")).resolve()
    if not node.is_dir():
        print(f"DLSS auto-setup: node not installed at {node}; skipping. Install it and restart the container.")
        return
    if not (node / "native/build_linux.sh").is_file():
        raise RuntimeError(f"The installed enhancer needs Linux worker support: {node}")
    # This variable overrides config.json inside the node, defeating managed
    # runtime selection after driver upgrades. Do not silently override the user.
    if os.environ.get("DLSS5_RUNTIME_DIR"):
        raise RuntimeError("Unset DLSS5_RUNTIME_DIR when using auto-setup; the helper manages runtime_dir in config.json.")
    state = private_dir(Path(os.environ.get("DLSS5_STATE_DIR") or "/comfy/mnt/dlss5").resolve())
    # Separate from prepare's setup.lock, so the installer and prepare together
    # are serialized across multiple simultaneous startup invocations.
    with lock(state / "startup.lock"):
        config_path = node / "config.json"
        config = read_config(config_path)
        source = Path(config.get("runtime_dir", node / "runtime")).resolve()
        if not all((source / name).is_file() for name in NODE_FILES[:2]):
            # Respect a user-selected external runtime rather than replacing it
            # with the default download when a mount is missing or incomplete.
            if source != (node / "runtime").resolve():
                raise RuntimeError(f"Configured runtime is missing/incomplete: {source}. Restore it or choose the node's runtime directory in config.json.")
            installer = node / "install_runtime.py"
            venv = os.environ.get("VIRTUAL_ENV")
            python = Path(venv) / "bin/python" if venv else None
            if not installer.is_file() or python is None or not python.is_file():
                raise RuntimeError("Auto runtime installation requires install_runtime.py and the activated ComfyUI venv.")
            print("DLSS auto-setup: installing the missing neural runtime using the node's installer (--yes).", flush=True)
            backup_config(config_path)
            run([str(python), str(installer), "--yes"], cwd=node, timeout=900)
            installed = read_config(config_path)
            # The node's installer replaces config.json. Retain user settings,
            # while selecting the runtime the installer actually populated.
            write_config(config_path, installed, {**config, "runtime_dir": str(source)})
        print("DLSS auto-setup: checking/preparing the persistent Wine setup.", flush=True)
        prepare(argparse.Namespace(node_dir=node, state_dir=state,
                                   runtime_dir=source, proton_dir=None,
                                   ngx_dir=None, download=True))


def main(wine=False):
    try:
        if wine:
            launch_wine(sys.argv[1:])
            return
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("node_dir", type=Path, nargs="?", help="Installed Linux-capable ComfyUI-DLSS5-Enhancer directory")
        parser.add_argument("--startup", action="store_true", help="Use DLSS5_AUTO_SETUP / DLSS5_NODE_DIR / DLSS5_STATE_DIR at container startup")
        parser.add_argument("--state-dir", type=Path, help="Persistent state, separate from all Python venvs (default: /comfy/mnt/dlss5)")
        parser.add_argument("--runtime-dir", type=Path, help="Already-installed neural runtime (never downloaded by this helper)")
        parser.add_argument("--proton-dir", type=Path, help="Extracted Proton root containing files/lib/wine")
        parser.add_argument("--ngx-dir", type=Path, help="Extracted matching NVIDIA driver package")
        parser.add_argument("--download", action="store_true", help="Allow downloading pinned Proton + matching driver archive for extraction only")
        args = parser.parse_args()
        if args.startup:
            if args.node_dir or args.state_dir or args.runtime_dir or args.proton_dir or args.ngx_dir or args.download:
                parser.error("--startup uses environment variables; do not combine it with manual setup options")
            if os.getuid() == 0 and os.environ.get("DLSS5_AUTO_SETUP", "false").lower() != "false":
                raise RuntimeError("Run auto-setup as the ComfyUI user, not root.")
            startup()
            return
        if args.node_dir is None:
            parser.error("node_dir is required for manual setup")
        if os.getuid() == 0:
            raise RuntimeError("Run as the ComfyUI user (docker exec --user comfy), not root.")
        args.state_dir = args.state_dir or Path("/comfy/mnt/dlss5")
        prepare(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"DLSS support: {exc}", file=sys.stderr)
        sys.exit(1)
