# DLSS-ready image validation — 2026-09-06

## Initial validation scope

Tested the new `components/part2-dlss5.Dockerfile` layer on the exact existing
Blackwell production image, not a full rebuild of the unchanged CUDA/Python base.
No production mounts were attached to either test container. No image was pushed,
no production container was restarted, and no host driver or production venv was
modified. The real enhancer checkout was not edited.

- GPU: NVIDIA RTX PRO 6000 Blackwell Workstation Edition, SM 12.0, 96 GB.
- Host NVIDIA driver: 595.84; Unraid 7.3.2, kernel 6.18.38.
- Base image ID: `sha256:6ecabb40f2daadda986c3dc11dd5eb83bb9bfad7abc31d7eef42c0f290124fa3`.
- Built test image: `comfy-dlss5-support-test:20260906`.
- Test image ID: `sha256:038e8d7b1ac90cf53fe5fbc75c8156c185c9f6c8e1a6328a33ddf1b93169d7c6`.
- Wine: 11.17 (`winehq-devel=11.17~noble-1`).
- Image MinGW compiler: GCC 13, win32 threading variant.
- Graphics source: Proton CachyOS `cachyos-11.0-20260521-slr`, baseline x86-64.
- Neural runtime SHA256: `6eb209e764f39872625debd6abaf45e2bb6322f6f270f781f70c059ae30b3927`.

The enhancer test copy was based on commit
`478badfca147a317082606bac529395868abe118`, with a **test-copy-only** GPU-name
compatibility patch for RTX PRO Blackwell / capability 12.0, plus two GPU-detection
regression tests. The separate agent's real GPU-name fix is still a prerequisite
for using an otherwise unpatched enhancer. This Docker change does not bypass
the enhancer's GPU checks.

## Results

| Check | Result |
| --- | --- |
| Docker dependency layer build | Passed; Wine and MinGW version checks passed |
| Native worker compilation inside the built image | Passed using the node's `native/build_linux.sh` |
| Pinned Proton download | Passed SHA256 verification; selected four regular DLL files |
| Cached NVIDIA package | Passed archive checks and `--extract-only`; no driver installation |
| NGX match | Extracted Linux NGX matched host-injected `/usr/lib64/libnvidia-ngx.so.1` byte-for-byte |
| Fresh managed prefix | Created as non-root `comfy`, separate from all Python venvs |
| Automatic display | Authenticated Xorg dummy, valid 60 Hz mode, no TCP listener |
| 512 × 512, three frames | Feature 18 verified, `native fallback=False` |
| Container restart, no manual Wine/display setup | Same prefix reused; launcher restarted Xorg successfully |
| 1280 × 720 → 1920 × 1080, three frames | Feature 18 verified, `native fallback=False` |
| Enhancer tests, including GPU intensity blending | All 6 passed, 17.665 seconds |
| Repeated setup using cached archives | Same bundle reused; no config replacement or prefix rebuild |
| CPU-only helper regression tests | All 12 passed |
| Generated Dockerfile consistency and `git diff --check` | Passed |

The helper tests cover explicit download consent, checksum failure, safe archive
selection, config preservation/backups, idempotence, driver validation, private
directory permissions, NGX library resolution/mismatch, unsupported displays,
stale-driver rejection and direct Wine execution without a stdout proxy.

The end-to-end image test used:

```text
runtime: /opt/dlss-probe/ready-state/bundles/driver-595.84-02c4ffaaac9de77e/runtime
prefix:  /opt/dlss-probe/ready-state/bundles/driver-595.84-02c4ffaaac9de77e/wineprefix
```

The 1080p output frame means were 97.15, 97.82 and 97.45. Native diagnostics
reported signed NR initialization, successful `CreateFeature(18)` and successful
`EvaluateFeature`, not merely a successful node import or D3D12 device creation.

## Automatic-setup follow-up

Added the opt-in `DLSS5_AUTO_SETUP=true` startup hook after the initial image
validation above. It runs after user scripts and venv activation, before ComfyUI,
and logs failures without aborting normal ComfyUI startup. The optional
`DLSS5_NODE_DIR` and `DLSS5_STATE_DIR` variables override its paths.

- All **24** CPU-only tests passed, including the first-install branch with a
  mocked installer, config preservation, existing-runtime reuse, disabled/missing
  node behavior, startup failure handling, and display selection.
- The exact shell hook extracted from `init.bash` was executed as `comfy` in the
  retained GPU test container, with the updated helper copied into it. This was
  not a rebuild of the image above or a full ComfyUI entrypoint execution.
- The hook reused the same managed bundle; the node config SHA256 remained
  `82cb9bd76aa4bc4c47c9b0e437f385d6fd86573b771a706cad8f6127348c1e81`.
- Repeated stop/start testing exposed a stale X11 lock whose PID could be reused
  by a new process. Automatic display selection now skips occupied/stale slots
  without deleting locks or stopping any existing display. This fixes a failure
  that was not seen during the first restart test above.
- After this fix and another container restart, the hook reused the prefix,
  selected display `:100`, and the 512 × 512 three-frame native self-test passed
  again with feature 18 verified and no fallback.

Tested helper SHA256:
`59f8f60cdac77b5c372d3c3573b3da2551968220c18d6fd826c95b4606c3d175`.
Tested `init.bash` SHA256:
`499eaba572c9d2441e5b6bbd516ec2714a04d55057278bfd95061cdb7aeb25e3`.
These follow-up changes are included in the published release below.

## Published release

The full release Dockerfile was built and pushed successfully by
[GitHub Actions run 34050806752](https://github.com/ethanfel/ComfyUI-Nvidia-Docker/actions/runs/34050806752)
on 2026-09-06. The job passed all 24 helper tests and completed in 7m13s.

- Source revision: `780c0c0ac06428259893280e983fcb4a24c7ed52`.
- Image: `ghcr.io/ethanfel/comfyui-nvidia-docker:blackwell-20260906-dlss5`.
- Matching public aliases: `blackwell`, `20260906-blackwell`, `cuda13.2-py3.13`.
- OCI index digest: `sha256:7c67c51fb38933a2b37eac0a63eaab53f323d9779b905ed525d6c35928ea25a3`.
- Linux amd64 manifest: `sha256:09f40b11bd4d94be5cb7c17b3a8ddd5728ae924ce46d93affb6593cd2c086480`.
- Image ID: `sha256:9d08d56e5747d294449c4ffdb85d7c121a418bd77ae2ff0e660d59b8378030f8`.

A complete pull using an empty Docker credential directory succeeded, confirming
anonymous access. A disposable, network-disabled local container then passed
entrypoint/config shell syntax checks, executable permission checks, helper
`--help`, and disabled `--startup` checks. Wine reported 11.17 and Python 3.13.14.
The entrypoint, config and helper hashes matched the release source exactly;
no `/opt/dlss-probe` test payload was present in the image. All four public aliases
resolved to the same manifests. The publishing operation did not update or
restart the production Unraid container.

## Boundaries and retained artifacts

This validates the native enhancer path on this driver/GPU combination. It is
not a long-video stability test, a performance benchmark, or a complete ComfyUI
workflow execution. Different Wine, Proton, neural-runtime or driver versions
need another end-to-end test.

Both disposable test containers are stopped and retained for inspection:
`comfy-dlss5-probe-20260906` and `comfy-dlss5-ready-test-20260906`.
The unpublished test image is also retained on the GPU host. Their copied test
venvs, prefixes and archives use additional disk space; no test artifacts were
deleted. The original production container remained running with its original
start time, `2026-09-06T11:34:17.313683863Z`.

See the [one-time setup instructions](../README.md#optional-dlss5-enhancer-support)
before deploying a newly built image.
