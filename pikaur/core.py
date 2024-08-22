"""Licensed under GPLv3, see https://www.gnu.org/licenses/"""

import codecs
import datetime
import os
import shutil
import subprocess  # nosec B404  # noqa: S404
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .args import parse_args
from .i18n import translate
from .pikaprint import ColorsHighlight, bold_line, color_line, print_error, print_stderr
from .privilege import sudo

if TYPE_CHECKING:
    # pylint: disable=cyclic-import
    from collections.abc import Sequence
    from typing import IO, Final, NotRequired

    from typing_extensions import TypedDict

    IOStream = IO[bytes] | int | None

    class SpawnArgs(TypedDict):
        stdout: NotRequired["IOStream"]
        stderr: NotRequired["IOStream"]
        cwd: NotRequired[str]
        env: NotRequired[dict[str, str]]


DEFAULT_INPUT_ENCODING: "Final" = "utf-8"
DEFAULT_TIMEZONE: "Final" = datetime.datetime.now().astimezone().tzinfo
PIPE: "Final" = subprocess.PIPE
READ_MODE: "Final" = "r"


class InteractiveSpawn(subprocess.Popen[bytes]):

    stdout_text: str | None
    stderr_text: str | None
    _terminated: bool = False

    def communicate(
            self, com_input: bytes | None = None, timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        if (
                parse_args().print_commands
                and not self._terminated
        ):
            print_stderr(
                color_line("=> ", ColorsHighlight.cyan) +
                (
                    " ".join(str(arg) for arg in self.args)
                    if isinstance(self.args, list) else
                    str(self.args)
                ),
                lock=False,
            )

        stdout, stderr = super().communicate(com_input, timeout)
        self.stdout_text = stdout.decode(DEFAULT_INPUT_ENCODING) if stdout else None
        self.stderr_text = stderr.decode(DEFAULT_INPUT_ENCODING) if stderr else None
        return stdout, stderr

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__} returned {self.returncode}:\n"
            f"STDOUT:\n{self.stdout_text}\n\n"
            f"STDERR:\n{self.stderr_text}"
        )

    def __del__(self) -> None:
        self.terminate()
        self._terminated = True
        self.communicate()


def interactive_spawn(
        cmd: list[str],
        stdout: "IOStream | None" = None,
        stderr: "IOStream | None" = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
) -> InteractiveSpawn:
    kwargs: SpawnArgs = {}
    if stdout:
        kwargs["stdout"] = stdout
    if stderr:
        kwargs["stderr"] = stderr
    if cwd:
        kwargs["cwd"] = str(cwd)
    if env:
        kwargs["env"] = env
    process = InteractiveSpawn(
        cmd, **kwargs,
    )
    process.communicate()
    return process


def spawn(
        cmd: list[str],
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
) -> InteractiveSpawn:
    with (
            tempfile.TemporaryFile() as out_file,
            tempfile.TemporaryFile() as err_file,
    ):
        proc = interactive_spawn(
            cmd, stdout=out_file, stderr=err_file,
            cwd=cwd, env=env,
        )
        out_file.seek(0)
        err_file.seek(0)
        proc.stdout_text = out_file.read().decode(DEFAULT_INPUT_ENCODING)
        proc.stderr_text = err_file.read().decode(DEFAULT_INPUT_ENCODING)
    return proc


def joined_spawn(
        cmd: list[str],
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
) -> InteractiveSpawn:
    with tempfile.TemporaryFile() as out_file:
        proc = interactive_spawn(
            cmd, stdout=out_file, stderr=out_file,
            cwd=cwd, env=env,
        )
        out_file.seek(0)
        proc.stdout_text = out_file.read().decode(DEFAULT_INPUT_ENCODING)
    return proc


class CodepageSequences:
    UTF_8: "Final[Sequence[bytes]]" = (b"\xef\xbb\xbf", )
    UTF_16: "Final[Sequence[bytes]]" = (b"\xfe\xff", b"\xff\xfe")
    UTF_32: "Final[Sequence[bytes]]" = (b"\xfe\xff\x00\x00", b"\x00\x00\xff\xfe")


def detect_bom_type(file_path: str | Path) -> str:
    """
    returns file encoding string for open() function
    https://stackoverflow.com/a/44295590/1850190
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)
    with file_path.open("rb") as test_file:
        first_bytes = test_file.read(4)

    if first_bytes[0:3] in CodepageSequences.UTF_8:
        return "utf-8"

    # Python automatically detects endianness if utf-16 bom is present
    # write endianness generally determined by endianness of CPU
    if first_bytes[0:2] in CodepageSequences.UTF_16:
        return "utf16"

    if first_bytes[0:5] in CodepageSequences.UTF_32:
        return "utf32"

    # If BOM is not provided, then assume its the codepage
    #     used by your operating system
    return "cp1252"
    # For the United States its: cp1252


def open_file(
        file_path: str | Path, mode: str = READ_MODE, encoding: str | None = None,
) -> codecs.StreamReaderWriter:
    if encoding is None and (mode and READ_MODE in mode):
        encoding = detect_bom_type(file_path)
    try:
        if encoding:
            return codecs.open(
                str(file_path), mode, errors="ignore", encoding=encoding,
            )
        return codecs.open(
            str(file_path), mode, errors="ignore",
        )
    except PermissionError:
        print_error()
        print_error(translate("Error opening file: {file_path}").format(file_path=file_path))
        print_error()
        raise


def replace_file(src: str | Path, dest: str | Path) -> None:
    if isinstance(src, str):
        src = Path(src)
    if isinstance(dest, str):
        dest = Path(dest)
    if src.exists():
        if dest.exists():
            dest.unlink()
        shutil.move(src, dest)


def remove_dir(dir_path: str | Path) -> None:
    try:
        shutil.rmtree(dir_path)
    except PermissionError:
        interactive_spawn(sudo(["rm", "-rf", str(dir_path)]))


def dirname(path: str | Path) -> Path:
    return Path(path).parent if path else Path()


def check_executables(dep_names: list[str]) -> None:
    for dep_bin in dep_names:
        if not shutil.which(dep_bin):
            message = translate("executable not found")
            print_error(
                f"'{bold_line(dep_bin)}' {message}.",
            )
            sys.exit(2)


def chown_to_current(path: Path) -> None:
    args = parse_args()
    user_id = args.user_id
    if user_id:
        if not isinstance(user_id, int):
            raise TypeError
        try:
            os.chown(path, user_id, user_id)
        except PermissionError as exc:
            print_error()
            print_error(
                translate("Can't change owner to {user_id}: {exc}").format(
                    user_id=user_id, exc=exc,
                ),
            )
            print_error()


def mkdir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True)
    chown_to_current(path)
