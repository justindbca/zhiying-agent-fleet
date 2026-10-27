"""WezTerm backend (design §4): the same interface as the cmux one, on top of a private mux server per run.

Written by the impl-wezterm branch; this module only declares why it cannot run yet so a
``--backend wezterm`` on a checkout without it fails with a readable reason instead of an ImportError.
"""
from __future__ import annotations


def availability_problem():
    return 'wezterm backend not available in this checkout: it is built on feat/backend-wezterm-wezterm'


class WezTermSessionManager:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(availability_problem())
