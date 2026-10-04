"""Colour maths: sRGB <-> linear RGB <-> CIELAB (D65), and colour correction."""

from __future__ import annotations

import numpy as np

_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]])
_WHITE = np.array([0.95047, 1.0, 1.08883])


def srgb_to_linear(rgb255) -> np.ndarray:
    c = np.asarray(rgb255, dtype=float) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(lin) -> np.ndarray:
    lin = np.clip(np.asarray(lin, dtype=float), 0.0, 1.0)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return c * 255.0


def linear_to_lab(lin) -> np.ndarray:
    lin = np.clip(np.asarray(lin, dtype=float), 0.0, None)
    xyz = lin @ _M.T / _WHITE
    d = 6 / 29
    f = np.where(xyz > d ** 3, np.cbrt(xyz), xyz / (3 * d * d) + 4 / 29)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def luminance(lin) -> np.ndarray:
    return np.asarray(lin, dtype=float) @ _M[1]


def fit_correction(observed_lin: np.ndarray, target_lin: np.ndarray) -> np.ndarray:
    """Affine 3×4 matrix mapping observed linear RGB to target linear RGB
    (least squares over the reference patches)."""
    X = np.column_stack([observed_lin, np.ones(len(observed_lin))])
    M, *_ = np.linalg.lstsq(X, target_lin, rcond=None)
    return M.T


def apply_correction(M: np.ndarray, lin: np.ndarray) -> np.ndarray:
    lin = np.asarray(lin, dtype=float)
    return lin @ M[:, :3].T + M[:, 3]
