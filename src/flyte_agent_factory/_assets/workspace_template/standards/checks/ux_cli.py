r"""Check that each declared command-line tool prints usage, rejects bad input and pipes plain output.

For each `[surfaces] cli` command the script runs three invocations: `<command> --help`,
`<command> <cli_invalid...>` and `<command> <cli_main...>`. Help must exit 0 and identify itself as usage (the
word "usage" or a line that starts with an option); the known-invalid invocation must exit non-zero and write
a message to stderr that names one of the rejected arguments or points to help; the main invocation (one
representative successful run; `cli_main = []` is the bare command) must exit 0. Whether the help is enough
to use the tool is not tested here.
The streams of all three are pipes, so the tool is not writing to a terminal: any ESC byte (colour or cursor
control) or carriage return not followed by a newline (a spinner or progress bar) in stdout or stderr fails.
Output is captured as bytes because text mode turns a lone "\r" into "\n" and hides the animation.
The environment is the caller's without NO_COLOR, FORCE_COLOR, CLICOLOR and CLICOLOR_FORCE, and with
TERM=xterm-256color: the only reason left for not colouring is that the output is a pipe, so a NO_COLOR in
the gate's container cannot rescue a tool that colours pipes and an unset TERM cannot pass one by accident.
Commands are the project's own executables, run without a shell from the project directory, each with a
time limit; a command that overruns is killed with everything it started. The worst case is three limits
per command (3 x COMMAND_SECONDS = 60 s), inside the gate's per-rule budget, so the limit is not raised.

Exit 0: pass, or NOT APPLICABLE (no CLI declared). Exit 1: fail. Run from the project directory.
"""

import os
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

# The standards directory is not the project's: importing a sibling must not leave __pycache__ in it.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ux_common as common  # type: ignore[import-not-found]  # noqa: E402 -- this directory is put on sys.path above

COMMAND_SECONDS = 20
EXCERPT_CHARS = 200
# Help may go to either stream (Go's flag package uses stderr) and may be headed "Usage", "usage:" or only
# list options; any of these shows the text is help rather than arbitrary output.
USAGE_WORD = re.compile(r"\busage\b", re.IGNORECASE)
OPTION_LINE = re.compile(r"^\s*-{1,2}[A-Za-z]", re.MULTILINE)
# "-h" must stand alone so that "-hello" or "x-h" does not count as a pointer to help.
POINTS_TO_HELP = re.compile(r"\busage\b|--help|(?<![\w-])-h(?![\w-])", re.IGNORECASE)
# A "\r" followed by "\n" is a Windows line ending; any other "\r" rewrites the line, which is animation.
BARE_CARRIAGE_RETURN = re.compile(rb"\r(?!\n)")
# Variables with which a person or a CI system switches colour on or off; the check must not depend on them.
COLOUR_SWITCHES = ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE")


def shorten_output(stdout: bytes | str, stderr: bytes | str) -> str:
    """Return a short one-line sample of what a command printed, for a failure line."""
    text = " ".join((_decode(stderr) + " " + _decode(stdout)).split())
    if not text:
        return "(no output)"
    return text[:EXCERPT_CHARS] + ("..." if len(text) > EXCERPT_CHARS else "")


def build_environment() -> dict[str, str]:
    """Return the caller's environment with colour switches removed and a colour-capable TERM."""
    environment = {k: v for k, v in os.environ.items() if k not in COLOUR_SWITCHES}
    environment["TERM"] = "xterm-256color"
    return environment


def _decode(data: bytes | str) -> str:
    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")


def run_command(argv: list[str], root: Path) -> tuple[int, bytes, bytes] | str:
    """Run argv in `root`; return (exit code, stdout, stderr bytes), or a problem string."""
    label = shlex.join(argv)
    try:
        # A session of its own lets the whole process group be killed: a tool that starts helpers must not
        # leave them behind.
        process = subprocess.Popen(
            argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=build_environment(), start_new_session=True,
        )  # fmt: skip
    except OSError as error:
        return f"{label}: cannot run {argv[0]}: {error.strerror or error}"
    try:
        stdout, stderr = process.communicate(timeout=COMMAND_SECONDS)
        return process.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return f"{label}: timed out after {COMMAND_SECONDS} s"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # everything has already exited
        process.communicate()


def find_piped_output_problems(shown: str, stdout: bytes, stderr: bytes) -> list[str]:
    """Return a line for each way the piped output of one invocation carries terminal control."""
    problems = []
    combined = stdout + b"\n" + stderr
    if b"\x1b" in combined:
        problems.append(
            f"{shown}: escape code in piped output (colour or cursor control; output is not a terminal): "
            f"{shorten_output(stdout, stderr)!r}"
        )
    if BARE_CARRIAGE_RETURN.search(stdout) or BARE_CARRIAGE_RETURN.search(stderr):
        problems.append(
            f"{shown}: carriage return without a newline in piped output (a spinner or progress bar?)"
        )
    return problems


def find_command_problems(root: Path, command: str, invalid: list[str], main: list[str]) -> list[str]:
    """Return one line per way `command` fails the help, bad-input, main-run and piped-output checks."""
    try:
        argv = shlex.split(command)
    except ValueError as error:
        return [f"{command}: cannot split into a command and arguments: {error}"]
    if not argv:
        return [f"{command!r}: is empty"]

    helped = run_command([*argv, "--help"], root)
    if isinstance(helped, str):
        return [helped]  # a command that is missing or hangs will not do better on bad input
    problems = []
    code, stdout, stderr = helped
    shown = shlex.join([*argv, "--help"])
    problems += find_piped_output_problems(shown, stdout, stderr)
    if code != 0:
        problems.append(f"{shown}: exit {code}, expected 0; output: {shorten_output(stdout, stderr)}")
    else:
        text = _decode(stdout) + "\n" + _decode(stderr)
        if not (USAGE_WORD.search(text) or OPTION_LINE.search(text)):
            problems.append(
                f"{shown}: exit 0 but the output does not look like usage "
                f"(no 'usage' and no option list such as '-h, --help'); output: "
                f"{shorten_output(stdout, stderr)}"
            )

    rejected = run_command([*argv, *invalid], root)
    if isinstance(rejected, str):
        return [*problems, rejected]
    code, stdout, stderr = rejected
    shown = shlex.join([*argv, *invalid])
    problems += find_piped_output_problems(shown, stdout, stderr)
    if code == 0:
        problems.append(
            f"{shown}: exit 0, expected non-zero for invalid input; output: {shorten_output(stdout, stderr)}"
        )
    else:
        message = _decode(stderr)
        if not (any(argument in message for argument in invalid) or POINTS_TO_HELP.search(message)):
            problems.append(
                f"{shown}: exit {code} but the stderr message names no rejected argument and does not point "
                f"to --help; stderr: {shorten_output('', stderr)}"
            )

    ran = run_command([*argv, *main], root)
    if isinstance(ran, str):
        return [*problems, ran]
    code, stdout, stderr = ran
    shown = shlex.join([*argv, *main])
    problems += find_piped_output_problems(shown, stdout, stderr)
    if code != 0:
        problems.append(
            f"{shown}: exit {code}, expected 0 for the main invocation; output: "
            f"{shorten_output(stdout, stderr)}"
        )
    return problems


def main() -> int:
    """Run the check in the current directory."""
    root = Path.cwd()
    config, _ = common.load_config(root)
    common.exit_if_malformed(config, root)
    commands = common.get_declared(config, "cli")
    if not commands:
        common.exit_not_applicable("no command-line tool declared ([surfaces] cli)")
    invalid = common.get_surfaces_table(config).get("cli_invalid")
    if invalid is None:
        print("FAIL: product.toml does not say what bad input to try")
        print(
            "  - surfaces.cli_invalid: missing; list arguments the tool must reject, "
            'e.g. ["--no-such-option"]'
        )
        return 1
    main = common.get_surfaces_table(config).get("cli_main")
    if main is None:
        print("FAIL: product.toml does not say how to run the tool successfully")
        print(
            "  - surfaces.cli_main: missing; list the arguments of one representative successful run, "
            'e.g. ["--version"] (an empty list runs the bare command)'
        )
        return 1
    problems = [p for command in commands for p in find_command_problems(root, command, invalid, main)]
    if not problems:
        print(
            f"PASS: {', '.join(commands)} prints usage for --help, rejects bad input with a message that "
            "names it (or points to --help), succeeds on its main invocation and writes no escape codes or "
            "carriage-return animation to a pipe"
        )
        return 0
    print("FAIL: a declared command-line tool does not behave as a CLI should")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
