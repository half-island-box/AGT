"""agt — local AI agent CLI bridge."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agt-team")
except PackageNotFoundError:  # source checkout without installation
    __version__ = "0.0.0.dev0"
