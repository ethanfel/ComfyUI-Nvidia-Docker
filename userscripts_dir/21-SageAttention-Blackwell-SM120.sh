#!/usr/bin/env bash

# SageAttention 2.2 for consumer/workstation Blackwell (SM120/SM120a).
#
# Intended for ComfyUI-Nvidia-Docker's /userscripts_dir. The script is:
#   - idempotent: a healthy native SM120a install is left untouched;
#   - ABI-aware: cached wheels are keyed by Python, Torch, CUDA and C++ ABI;
#   - persistent: source and wheels live outside the venv under /comfy/mnt;
#   - conservative: it builds only SM120a and never installs dependencies.

set -Eeuo pipefail

readonly SAGE_REPO="${SAGEATTENTION_REPO:-https://github.com/thu-ml/SageAttention.git}"
readonly SAGE_REF="${SAGEATTENTION_REF:-d1a57a546c3d395b1ffcbeecc66d81db76f3b4b5}"
readonly VENV="${SAGEATTENTION_VENV:-/comfy/mnt/venv}"
readonly PYTHON="${VENV}/bin/python"
readonly CACHE_ROOT="${SAGEATTENTION_CACHE_ROOT:-/comfy/mnt/sageattention-blackwell}"
readonly SOURCE_ROOT="${CACHE_ROOT}/source"
readonly WHEEL_ROOT="${CACHE_ROOT}/wheels"
readonly VALIDATION_ROOT="${CACHE_ROOT}/validation"
readonly MAX_JOBS="${SAGEATTENTION_MAX_JOBS:-8}"
readonly EXT_PARALLEL="${SAGEATTENTION_EXT_PARALLEL:-2}"

export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

log() {
  printf '[SageAttention SM120] %s\n' "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

[[ -x "$PYTHON" ]] || die "Python was not found at ${PYTHON}"
[[ -x "${CUDA_HOME}/bin/nvcc" ]] || die "nvcc was not found under ${CUDA_HOME}"
command -v git >/dev/null 2>&1 || die "git is required"
command -v ninja >/dev/null 2>&1 || "$PYTHON" -c 'import ninja' >/dev/null 2>&1 || die "ninja is required"
command -v cuobjdump >/dev/null 2>&1 || die "cuobjdump is required to validate native cubins"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required to cache binary validation"

runtime_info="$({ "$PYTHON" - <<'PY'
import sys
import torch

if not torch.cuda.is_available():
    raise SystemExit("CUDA is not available")
major, minor = torch.cuda.get_device_capability(0)
if (major, minor) != (12, 0):
    raise SystemExit(f"this script only supports SM120; found SM{major}{minor}")
print("|".join((
    sys.implementation.cache_tag,
    torch.__version__,
    str(torch.version.cuda),
    f"{major}.{minor}",
    str(int(torch._C._GLIBCXX_USE_CXX11_ABI)),
)))
PY
} 2>&1)" || die "$runtime_info"

IFS='|' read -r python_tag torch_version torch_cuda arch cxx11_abi <<<"$runtime_info"
[[ -n "$python_tag" && -n "$torch_version" && -n "$torch_cuda" && "$arch" == "12.0" ]] \
  || die "could not determine a valid build fingerprint: ${runtime_info}"

safe_component() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9._+-' '_'
}

ref_id="${SAGE_REF:0:12}"
fingerprint="$(safe_component "${ref_id}-${python_tag}-torch${torch_version}-cu${torch_cuda}-sm${arch}-abi${cxx11_abi}")"
wheel_dir="${WHEEL_ROOT}/${fingerprint}"
source_dir="${SOURCE_ROOT}/SageAttention"
validation_marker="${VALIDATION_ROOT}/${fingerprint}.sha256"

validate_install() {
  local extensions extension_hash so
  extensions="$({ "$PYTHON" - <<'PY'
import importlib
import torch
import sageattention

if torch.cuda.get_device_capability(0) != (12, 0):
    raise SystemExit("not an SM120 device")
for name in (
    "sageattention._qattn_sm80",
    "sageattention._qattn_sm89",
    "sageattention._fused",
):
    module = importlib.import_module(name)
    print(module.__file__)
PY
  } 2>/dev/null)" || return 1

  extension_hash="$(
    while IFS= read -r so; do
      [[ -f "$so" ]] || return 1
      sha256sum "$so"
    done <<<"$extensions" | sha256sum | awk '{print $1}'
  )"

  if [[ -f "$validation_marker" ]] \
    && [[ "$(<"$validation_marker")" == "$extension_hash" ]]; then
    return 0
  fi

  while IFS= read -r so; do
    [[ -f "$so" ]] || return 1
    # Do not use grep -q here: with pipefail it can SIGPIPE cuobjdump.
    cuobjdump -lelf "$so" 2>/dev/null | grep -F 'sm_120a' >/dev/null || return 1
  done <<<"$extensions"

  mkdir -p "$VALIDATION_ROOT"
  printf '%s\n' "$extension_hash" >"$validation_marker"
}

smoke_test() {
  "$PYTHON" - <<'PY'
import torch
import torch.nn.functional as F
from sageattention import sageattn

torch.manual_seed(120)
q = torch.randn(1, 4, 256, 128, device="cuda", dtype=torch.float16)
k = torch.randn_like(q)
v = torch.randn_like(q)
out = sageattn(q, k, v, tensor_layout="HND", is_causal=False)
ref = F.scaled_dot_product_attention(q, k, v)
torch.cuda.synchronize()
delta = (out - ref).abs().float()
if not bool(torch.isfinite(out).all()):
    raise SystemExit("SageAttention produced non-finite output")
if delta.mean().item() >= 0.01 or delta.max().item() >= 0.15:
    raise SystemExit(
        f"SageAttention correctness check failed: mean={delta.mean().item():.6f}, "
        f"max={delta.max().item():.6f}"
    )
print(
    "[SageAttention SM120] GPU smoke test passed: "
    f"mean_abs_error={delta.mean().item():.6f}, max_abs_error={delta.max().item():.6f}"
)
PY
}

log "Runtime: Python=${python_tag}, Torch=${torch_version}, Torch CUDA=${torch_cuda}, SM120, ABI=${cxx11_abi}"

if validate_install; then
  log "A healthy native SM120a installation is already available; nothing to do"
  exit 0
fi

mkdir -p "$wheel_dir" "$SOURCE_ROOT"

wheel="$(find "$wheel_dir" -maxdepth 1 -type f -name 'sageattention-*.whl' -print -quit)"
if [[ -n "$wheel" ]]; then
  log "Installing cached ABI-matched wheel: ${wheel}"
  "$PYTHON" -m pip install --no-deps --force-reinstall "$wheel"
  validate_install || die "the cached wheel did not contain loadable native SM120a extensions"
  smoke_test
  exit 0
fi

log "No wheel exists for fingerprint ${fingerprint}; compiling one"

if [[ ! -d "${source_dir}/.git" ]]; then
  git clone --filter=blob:none --no-checkout "$SAGE_REPO" "$source_dir"
fi

git -C "$source_dir" fetch --depth 1 origin "$SAGE_REF"
git -C "$source_dir" checkout --detach --force FETCH_HEAD

build_dir="$(mktemp -d /tmp/sageattention-sm120.XXXXXX)"
cleanup() {
  rm -rf -- "$build_dir"
}
trap cleanup EXIT

git -C "$source_dir" archive HEAD | tar -xf - -C "$build_dir"

log "Building ${SAGE_REF} with TORCH_CUDA_ARCH_LIST=12.0, MAX_JOBS=${MAX_JOBS}, EXT_PARALLEL=${EXT_PARALLEL}"
CUDA_HOME="$CUDA_HOME" \
TORCH_CUDA_ARCH_LIST='12.0' \
MAX_JOBS="$MAX_JOBS" \
EXT_PARALLEL="$EXT_PARALLEL" \
  "$PYTHON" -m pip wheel "$build_dir" \
    --no-build-isolation \
    --no-deps \
    --wheel-dir "$wheel_dir"

wheel="$(find "$wheel_dir" -maxdepth 1 -type f -name 'sageattention-*.whl' -print -quit)"
[[ -n "$wheel" ]] || die "the build completed without producing a wheel"

log "Installing newly built wheel: ${wheel}"
"$PYTHON" -m pip install --no-deps --force-reinstall "$wheel"
validate_install || die "the new wheel did not contain loadable native SM120a extensions"
smoke_test
log "SageAttention is ready"
