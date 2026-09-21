"""The desktop application.

Everything here is optional: the package is importable without PySide6, because
:mod:`gigaxml.gui.progress` and :mod:`gigaxml.gui.cli_process` do not need it and are
where the logic that can actually be tested lives. Only the modules that build windows
import Qt, and only the ``gui`` extra installs it.
"""
