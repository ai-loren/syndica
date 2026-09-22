"""Command-line surface.

Keep this package initializer free of kernel, backend, and YAML imports so the
version command can remain a genuinely lazy path.
"""

from syndica.cli.main import CliHost, CliIO, InspectRequest, RunRequest, main

__all__ = ['CliHost', 'CliIO', 'InspectRequest', 'RunRequest', 'main']
