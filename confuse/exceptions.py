from yaml.scanner import ScannerError

__all__ = [
    "ConfigConflictError",
    "ConfigError",
    "ConfigReadError",
    "ConfigTemplateError",
    "ConfigTypeError",
    "ConfigValueError",
    "ConfigWriteError",
    "NotFoundError",
]

YAML_TAB_PROBLEM = "found character '\\t' that cannot start any token"

# Exceptions.


class ConfigError(Exception):
    """Base class for exceptions raised when querying a configuration."""


class NotFoundError(ConfigError):
    """A requested value could not be found in the configuration trees."""


class ConfigValueError(ConfigError):
    """The value in the configuration is illegal."""


class ConfigTypeError(ConfigValueError):
    """The value in the configuration did not match the expected type."""


class ConfigTemplateError(ConfigError):
    """Base class for exceptions raised because of an invalid template."""


class ConfigReadError(ConfigError):
    """A configuration source could not be read."""

    def __init__(self, name: str, reason: Exception | None = None) -> None:
        self.name = name
        self.reason = reason

        message = f"{name} could not be read"
        if (
            isinstance(reason, ScannerError)
            and reason.problem == YAML_TAB_PROBLEM
            and reason.problem_mark
        ):
            # Special-case error message for tab indentation in YAML markup.
            message += (
                f": found tab character at line {reason.problem_mark.line + 1}, "
                f"column {reason.problem_mark.column + 1}"
            )
        elif reason:
            # Generic error message uses exception's message.
            message += f": {reason}"

        super().__init__(message)


class ConfigConflictError(ConfigError):
    """A configuration file changed on disk while it was being edited.

    Raised by an editing session when the target YAML source was
    modified externally after the session began, so persisting the
    session's changes would risk clobbering the other modification.
    """

    def __init__(self, filename: str | None) -> None:
        self.filename = filename
        if filename:
            message = f"{filename} changed on disk during the editing session"
        else:
            message = "configuration source changed during the editing session"
        super().__init__(message)


class ConfigWriteError(ConfigError):
    """A configuration file could not be written atomically.

    The original file is left untouched when this is raised.
    """

    def __init__(self, filename: str, reason: Exception | None = None) -> None:
        self.filename = filename
        self.reason = reason
        message = f"{filename} could not be written"
        if reason:
            message += f": {reason}"
        super().__init__(message)
