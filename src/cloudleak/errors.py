"""Error types. Every user-facing failure carries an exit code."""

from __future__ import annotations

EXIT_OK = 0
EXIT_FINDINGS = 1  # command succeeded and found something the policy treats as a failure
EXIT_INPUT = 2  # fail closed: input cannot support the requested analysis


class CloudLeakError(Exception):
    """A failure the user can act on. Never shown with a traceback."""

    exit_code = EXIT_INPUT

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class InputError(CloudLeakError):
    """The input files cannot be read safely or completely."""


class RedactionLeakError(CloudLeakError):
    """The residual-identifier gate found something that must not leave the machine."""
