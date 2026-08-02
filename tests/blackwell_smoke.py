#!/usr/bin/env python3
"""Small GPU smoke test for the published RTX PRO 6000 image."""

from __future__ import annotations

import json
import platform

import torch
import torch.nn.functional as functional
import torchaudio
import torchvision


def main() -> None:
    assert platform.python_version_tuple()[:2] == ("3", "13"), platform.python_version()
    assert torch.__version__.startswith("2.13.0+cu130"), torch.__version__
    assert torchvision.__version__.startswith("0.28.0+cu130"), torchvision.__version__
    assert torchaudio.__version__.startswith("2.11.0+cu130"), torchaudio.__version__
    assert torch.version.cuda == "13.0", torch.version.cuda
    assert torch.cuda.is_available(), "CUDA is not available"
    assert "sm_120" in torch.cuda.get_arch_list(), torch.cuda.get_arch_list()

    device = torch.cuda.current_device()
    capability = torch.cuda.get_device_capability(device)
    assert capability == (12, 0), capability

    left = torch.randn((2048, 2048), device="cuda", dtype=torch.float16)
    right = torch.randn((2048, 2048), device="cuda", dtype=torch.float16)
    product = left @ right
    assert torch.isfinite(product).all().item()

    query = torch.randn((1, 16, 1024, 128), device="cuda", dtype=torch.float16)
    attention = functional.scaled_dot_product_attention(query, query, query)
    assert torch.isfinite(attention).all().item()
    torch.cuda.synchronize()

    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "torch": torch.__version__,
                "torchvision": torchvision.__version__,
                "torchaudio": torchaudio.__version__,
                "torch_cuda": torch.version.cuda,
                "gpu": torch.cuda.get_device_name(device),
                "capability": ".".join(map(str, capability)),
                "matmul": "ok",
                "sdpa": "ok",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
