"""turn-completion-nudge plugin package.

The root ``__init__.py`` exposing ``register`` is required or the Hermes loader
silently skips the plugin.
"""

try:
    from .src.turn_completion_nudge import register
except ImportError:  # e.g. pytest package setup
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from src.turn_completion_nudge import register

__all__ = ["register"]
