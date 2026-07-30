# SPDX-FileCopyrightText: 2026 Ippocra S.r.l.
# SPDX-License-Identifier: Apache-2.0

"""ilai-sentinel — local reporting daemon for deployed ILAI units."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ilai-sentinel")
except PackageNotFoundError:
    try:
        __version__ = import_module("sentinel._version").__version__
    except ImportError:
        __version__ = "0+unknown"
