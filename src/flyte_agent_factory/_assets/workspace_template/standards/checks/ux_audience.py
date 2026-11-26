"""Check that product.toml names the audience, and the pages to render when there is a UI.

A project with UI files or a command-line entry point (pyproject.toml [project.scripts], package.json "bin")
has users, so it needs product.toml even when nobody wrote one.

Exit 0: pass, or NOT APPLICABLE (no UI files, no CLI entry point, and no product.toml declaring pages, text,
a CLI or an audience: a product.toml with only [brand] has no users to name). Exit 1: fail.
Run from the project directory.
"""

import sys
from pathlib import Path

# The standards directory is not the project's: importing a sibling must not leave __pycache__ in it.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ux_common as common  # type: ignore[import-not-found]  # noqa: E402 -- this directory is put on sys.path above


def main() -> int:
    """Run the check in the current directory."""
    root = Path.cwd()
    config, parse_error = common.load_config(root)
    ui = common.find_ui_files(root)
    cli = common.detect_cli(root)
    has_toml = (root / "product.toml").is_file()
    # An unparsable or malformed product.toml might declare anything, so it fails rather than passing as not
    # applicable, as the other three scripts do.
    malformed = bool(parse_error) or bool(common.find_surface_problems(config, root))
    if not malformed and not common.has_surface(root, config):
        common.exit_not_applicable(
            "no human-facing surface (no UI files, no CLI entry point, and no product.toml declaring pages, "
            "text, a CLI or an audience)"
        )
    problems = [parse_error] if parse_error else []
    if not has_toml and cli:
        problems.append(
            "product.toml: missing at the project root, but the project has a command-line entry point "
            f"({cli}); a CLI has users: add product.toml naming them, and the command under [surfaces] cli"
        )
    elif not has_toml:
        problems.append("product.toml: missing at the project root")
    # With no file, `config` is None and the audience problems are the five missing fields, named one by one.
    if not parse_error:
        problems += common.find_surface_problems(config, root) + common.find_audience_problems(config)
    if ui and not common.get_declared_pages(config):
        problems.append(
            f"surfaces.pages: not declared, but the project has UI files (first: {ui[0].relative_to(root)}); "
            "declare the pages to render"
        )
    if not problems:
        print("PASS: product.toml names the audience")
        return 0
    print("FAIL: product.toml is incomplete")
    for problem in problems:
        print(f"  - {problem}")
    print("Minimal example:")
    print("\n".join(f"  {line}" for line in common.EXAMPLE.splitlines()))
    return 1


if __name__ == "__main__":
    sys.exit(main())
