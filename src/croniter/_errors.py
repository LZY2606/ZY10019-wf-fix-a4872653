"""Exception hierarchy for croniter.

This module is the internal home of the public exception types. They are
re-exported from ``croniter.croniter`` (and ``croniter``), and their
``__module__`` is pinned below so that the user-visible repr and pickle
paths keep referring to ``croniter.croniter`` exactly as before the
internal component split.
"""


class CroniterError(ValueError):
    """General top-level Croniter base exception"""


class CroniterBadTypeRangeError(TypeError):
    """."""


class CroniterBadCronError(CroniterError):
    """Syntax, unknown value, or range error within a cron expression"""


class CroniterUnsupportedSyntaxError(CroniterBadCronError):
    """Valid cron syntax, but likely to produce inaccurate results"""

    # Extending CroniterBadCronError, which may be contridatory, but this allows
    # catching both errors with a single exception.  From a user perspective
    # these will likely be handled the same way.


class CroniterBadDateError(CroniterError):
    """Unable to find next/prev timestamp match"""


class CroniterNotAlphaError(CroniterBadCronError):
    """Cron syntax contains an invalid day or month abbreviation"""


for _exc in (
    CroniterError,
    CroniterBadTypeRangeError,
    CroniterBadCronError,
    CroniterUnsupportedSyntaxError,
    CroniterBadDateError,
    CroniterNotAlphaError,
):
    _exc.__module__ = "croniter.croniter"

del _exc
