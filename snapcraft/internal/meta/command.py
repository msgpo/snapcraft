# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2019 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import re
import shlex
import shutil
from typing import List, Optional, Tuple

from . import errors
from ._utils import _executable_is_valid
from snapcraft.internal import common


logger = logging.getLogger(__name__)
_COMMAND_PATTERN = re.compile("^[A-Za-z0-9. _#:$-][A-Za-z0-9/. _#:$-]*$")
_FMT_SNAPD_WRAPPER = (
    "A shell wrapper will be generated for command {!r} as it does not conform "
    "with the command pattern expected by the runtime. "
    "Commands must be relative to the prime directory and can only consist "
    "of alphanumeric characters, spaces, and the following special characters: "
    "/ . _ # : $ -"
)


def _get_shebang_from_file(file_path: str) -> Optional[str]:
    """Returns the shebang from file_path."""
    with open(file_path, "rb") as exefile:
        if exefile.read(2) != b"#!":
            return None
        shebang_line = exefile.readline().strip().decode("utf-8")

    return shebang_line


def _find_executable(*, command: str, prime_dir: str) -> Optional[str]:
    binary_paths = (
        os.path.join(p, command) for p in common.get_bin_paths(root=prime_dir)
    )
    for binary_path in binary_paths:
        if _executable_is_valid(binary_path):
            return binary_path

    # Last chance to find in the prime_dir, mostly for backwards compatibility,
    # to find the executable, historical snaps like those built with the catkin
    # plugin will have roslaunch in a path like /opt/ros/bin/roslaunch.
    for root, _, files in os.walk(prime_dir):
        if _executable_is_valid(os.path.join(root, command)):
            return os.path.join(root, command)

    # Finally, check if it is part of the system.
    return shutil.which(command)


def _resolve_snap_command_path(
    *, command: str, prime_dir: str
) -> Tuple[Optional[str], bool]:
    search_required = False
    command = _strip_command_leaders(command)

    # If it is where it claims to be, search for backwards compatibility.
    if not os.path.exists(os.path.join(prime_dir, command)):
        search_required = True

        found_command = _find_executable(command=command, prime_dir=prime_dir)
        if found_command is None:
            return found_command, search_required

        if found_command.startswith(prime_dir):
            command = os.path.relpath(found_command, prime_dir)
        else:
            command = found_command

    return command, search_required


def _strip_command_leaders(command: str) -> str:
    # Strip leading "/"
    command = re.sub(r"^/", "", command)

    # Strip leading "$SNAP/"
    command = re.sub(r"^\$SNAP/", "", command)

    return command


def _split_command(*, command: str, prime_dir: str) -> Tuple[List[str], List[str]]:
    """Parse command, returning (interpreter, command)."""

    # posix is set to False to respect the quoting of variables.
    command_parts = shlex.split(command, posix=False)
    command_path = os.path.join(prime_dir, _strip_command_leaders(command_parts[0]))

    shebang_parts: List[str] = list()
    if os.path.exists(command_path):
        shebang = _get_shebang_from_file(command_path)
        if shebang:
            shebang_parts = shlex.split(shebang, posix=False)

    return shebang_parts, command_parts


def _resolve_interpreter_parts(
    *, shebang_parts: List[str], command_parts: List[str], prime_dir: str
) -> List[str]:
    # Remove the leading /usr/bin/env and resolve it now.
    resolved_parts = shebang_parts.copy()
    if resolved_parts[0] == "/usr/bin/env":
        resolved_parts = resolved_parts[1:]

    resolved_interpreter, search_required = _resolve_snap_command_path(
        command=resolved_parts[0], prime_dir=prime_dir
    )

    if resolved_interpreter is None:
        # Note this is not a hard error, just warn.
        logger.warning("Unable to find interpreter in any paths: {resolved_parts[0]!r}")
    elif search_required:
        logger.warning(
            f"The interpreter {resolved_parts[0]!r} for {command_parts[0]!r} was resolved to {resolved_interpreter!r}."
        )

    if resolved_interpreter is not None:
        resolved_parts[0] = resolved_interpreter

    return resolved_parts


def _resolve_command_parts(
    *, command_parts: List[str], interpreted_command: bool, prime_dir: str
) -> List[str]:
    resolved_parts = command_parts.copy()
    resolved_command, search_required = _resolve_snap_command_path(
        command=resolved_parts[0], prime_dir=prime_dir
    )

    if resolved_command is None:
        raise errors.PrimedCommandNotFoundError(resolved_parts[0])
    elif search_required:
        logger.warning(
            f"The command {resolved_parts[0]!r} was not found in the prime directory, but found {resolved_command!r}."
        )

    # Prepend $SNAP now required for resolved command.
    if (
        interpreted_command
        and resolved_command
        and not resolved_command.startswith("/")
    ):
        resolved_command = os.path.join("$SNAP", resolved_command)

    resolved_parts[0] = resolved_command
    return resolved_parts


def _massage_command(*, command: str, prime_dir: str) -> str:
    """Rewrite command to take into account interpreter and pathing.

    (1) Interpreter: if shebang is found in file, explicitly prepend
        the interpreter to the command string.  Fixup path of
        command with $SNAP if required so the interpreter is able
        to find the correct target.
    (2) Explicit path: attempt to find the executable if path
        is ambiguous.  If found in prime_dir, set the path relative
        to snap.

    Returns massaged command."""

    # If command starts with "/" we have no option but to use a wrapper.
    if command.startswith("/"):
        return command

    massaged_command = re.sub(r"^\$SNAP/", "", command)
    shebang_parts, command_parts = _split_command(
        command=massaged_command, prime_dir=prime_dir
    )

    if shebang_parts:
        interpreted_command = True
        shebang_parts = _resolve_interpreter_parts(
            shebang_parts=shebang_parts,
            command_parts=command_parts,
            prime_dir=prime_dir,
        )
    else:
        interpreted_command = False

    command_parts = _resolve_command_parts(
        command_parts=command_parts,
        interpreted_command=interpreted_command,
        prime_dir=prime_dir,
    )

    massaged_command = " ".join(shebang_parts + command_parts)

    # Inform the user of any changes.
    if massaged_command != command:
        # Make a note now that $SNAP if found this path will not make it into the
        # resulting command entry.
        if command.startswith("$SNAP/"):
            logger.warning(f"Found unneeded '$SNAP/' in command {command!r}.")

        if interpreted_command:
            logger.warning(
                f"The command {command!r} has been changed to {massaged_command!r} to safely account for the interpreter."
            )
        else:
            logger.warning(
                f"The command {command!r} has been changed to {massaged_command!r}."
            )

    return massaged_command


class Command:
    """Representation of a command string."""

    def __str__(self) -> str:
        return self.command

    def __init__(self, *, app_name: str, command_name: str, command: str) -> None:
        self._app_name = app_name
        self._command_name = command_name
        self.command = command
        self.wrapped_command: Optional[str] = None
        self.massaged_command: Optional[str] = None

    @property
    def command_name(self) -> str:
        """Read-only to ensure consistency with app dictionary mappings."""
        return self._command_name

    @property
    def requires_wrapper(self) -> bool:
        if self.wrapped_command is not None:
            command = self.wrapped_command
        else:
            command = self.command

        return command.startswith("/") or not _COMMAND_PATTERN.match(command)

    @property
    def wrapped_command_name(self) -> str:
        """Return the relative in-snap path to the wrapper for command."""
        return "{command_name}-{app_name}.wrapper".format(
            command_name=self.command_name, app_name=self._app_name
        )

    def prime_command(
        self, *, can_use_wrapper: bool, massage_command: bool = True, prime_dir: str,
    ) -> str:
        """Finalize and prime command, massaging as necessary.

        Check if command is in prime_dir and raise exception if not valid."""

        if massage_command:
            self.command = _massage_command(command=self.command, prime_dir=prime_dir)

        if self.requires_wrapper:
            if not can_use_wrapper:
                raise errors.InvalidAppCommandFormatError(self.command, self._app_name)
            self.wrapped_command = self.command
            if not _COMMAND_PATTERN.match(self.command):
                logger.warning(_FMT_SNAPD_WRAPPER.format(self.command))
            self.command = self.wrapped_command_name
        else:
            command_parts = shlex.split(self.command)
            command_path = os.path.join(prime_dir, command_parts[0])
            if not _executable_is_valid(command_path):
                raise errors.InvalidAppCommandNotExecutable(
                    command=self.command, app_name=self._app_name
                )

        return self.command

    def write_wrapper(self, *, prime_dir: str) -> Optional[str]:
        """Write command wrapper if required for this command."""
        if self.wrapped_command is None:
            return None

        command_wrapper = os.path.join(prime_dir, self.wrapped_command_name)

        # We cannot exec relative paths in our wrappers.
        if self.wrapped_command.startswith("/"):
            command = self.wrapped_command
        else:
            command = os.path.join("$SNAP", self.wrapped_command)

        with open(command_wrapper, "w+") as command_file:
            print("#!/bin/sh", file=command_file)
            print('exec {} "$@"'.format(command), file=command_file)

        os.chmod(command_wrapper, 0o755)
        return command_wrapper
