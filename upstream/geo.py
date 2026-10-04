"""Small geodesy helpers: a local equirectangular projection is accurate to
well under a metre over the few kilometres a ward covers."""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_M = 6_371_008.8


def project(lat, lon, origin: tuple[float, float]):
    """Project WGS84 degrees to local metres (x east, y north) around ``origin``."""
    lat0, lon0 = origin
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    k = math.pi / 180.0 * EARTH_RADIUS_M
    x = (lon - lon0) * k * math.cos(math.radians(lat0))
    y = (lat - lat0) * k
    return x, y


def unproject(x, y, origin: tuple[float, float]):
    """Inverse of :func:`project`."""
    lat0, lon0 = origin
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    k = math.pi / 180.0 * EARTH_RADIUS_M
    lat = lat0 + y / k
    lon = lon0 + x / (k * math.cos(math.radians(lat0)))
    return lat, lon


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (vectorised)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
