"""Safely update one variable (``FYERS_ACCESS_TOKEN``) in a local ``.env`` file.

Guarantees:

* Only the target variable changes. Every other line -- comments, blank lines, order,
  ``export`` prefixes, line endings, other secrets such as ``FYERS_APP_SECRET`` -- is
  preserved byte for byte. All active definitions of the variable are updated (the
  credential loader uses the last one); commented-out lines are left alone. A missing
  variable is appended.
* Symlinks are never followed: a symlinked ``.env`` is refused (checked with ``lstat``
  and ``O_NOFOLLOW``). The file must be a regular file, owned by the current user, with a
  single hard link.
* The new content is written to a private temp file (mode ``0600``, ``O_EXCL``) in the
  same directory, fsync'ed, then atomically renamed over ``.env``. The result always
  has mode ``0600``. ``os.replace`` swaps the directory entry itself, so even a symlink
  planted after the checks is replaced rather than followed.
* If git positively reports the file is not ignored (or is tracked), the update is
  refused so a token cannot be committed by accident. If git is unavailable or the
  directory is not a repository, the check is skipped.

The value is validated against a strict character set and is never logged.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from .errors import EnvFileError

logger = logging.getLogger(__name__)

MAX_ENV_BYTES = 1024 * 1024
_VALUE_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,4096}")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_POSIX = os.name == "posix"


def _check_git_ignored(path: Path) -> None:
    try:
        outcome = subprocess.run(
            ["git", "check-ignore", "-q", "--", path.name],
            cwd=path.parent, capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return  # git missing/unusable: cannot tell, do not block
    if outcome.returncode == 1:  # 0 = ignored, 128 = not a repository / error
        raise EnvFileError(
            f"{path.name} is not ignored by git (or is tracked); add it to .gitignore "
            "before storing a token in it"
        )


def _lstat_checks(path: Path) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EnvFileError(f"cannot inspect {path.name}: {type(exc).__name__}") from None
    if stat.S_ISLNK(info.st_mode):
        raise EnvFileError(f"{path.name} is a symbolic link; refusing to follow it")
    return info


def _check_regular_owned(info: os.stat_result, name: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise EnvFileError(f"{name} is not a regular file")
    if info.st_size > MAX_ENV_BYTES:
        raise EnvFileError(f"{name} is unexpectedly large")
    if _POSIX:
        if info.st_uid != os.geteuid():
            raise EnvFileError(f"{name} is not owned by the current user")
        if info.st_nlink > 1:
            raise EnvFileError(f"{name} has multiple hard links; refusing to modify it")


def preflight(path: str | Path) -> None:
    """Raise :class:`EnvFileError` if ``path`` could not be updated safely.

    Run this *before* starting a login so a completed login is never lost to a
    storage problem.
    """
    path = Path(path)
    if not path.parent.is_dir():
        raise EnvFileError(f"directory for {path.name} does not exist")
    info = _lstat_checks(path)
    if info is not None:
        _check_regular_owned(info, path.name)
    elif not os.access(path.parent, os.W_OK):
        raise EnvFileError(f"cannot create {path.name}: directory is not writable")
    _check_git_ignored(path)


def _read_existing(path: Path) -> str | None:
    if _lstat_checks(path) is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EnvFileError(f"cannot open {path.name} safely: {type(exc).__name__}") from None
    with os.fdopen(fd, "rb") as handle:
        _check_regular_owned(os.fstat(handle.fileno()), path.name)
        data = handle.read(MAX_ENV_BYTES + 1)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EnvFileError(f"{path.name} is not valid UTF-8; refusing to rewrite it") from None


def _replace_or_append(text: str, name: str, value: str) -> str:
    pattern = re.compile(rf"^([ \t]*(?:export[ \t]+)?){re.escape(name)}[ \t]*=")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = f"{match.group(1)}{name}={value}{ending}"
            found = True
    if found:
        return "".join(lines)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    lines.append(f"{name}={value}{newline}")
    return "".join(lines)


def _write_private(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # 0600, O_EXCL
    try:
        with os.fdopen(fd, "wb") as handle:
            if _POSIX:
                os.fchmod(handle.fileno(), 0o600)
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def set_env_value(path: str | Path, name: str, value: str) -> None:
    """Set ``name=value`` in ``path`` (see module docstring for the safety guarantees)."""
    path = Path(path)
    if not _NAME_RE.fullmatch(name):
        raise EnvFileError("invalid variable name")
    if not _VALUE_RE.fullmatch(value):
        raise EnvFileError(f"refusing to write {name}: value has an unexpected format")
    preflight(path)
    existing = _read_existing(path)
    updated = _replace_or_append(existing or "", name, value)
    try:
        _write_private(path, updated)
    except OSError as exc:
        raise EnvFileError(f"could not write {path.name}: {type(exc).__name__}") from None
    logger.info("updated %s in %s (mode 0600)", name, path)
