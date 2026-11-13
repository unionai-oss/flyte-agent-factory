"""`promote.toml` is the operator's: no session edit of it is in scope, whatever the brief permits."""

from __future__ import annotations

import pytest

from flyte_agent_factory import _gate_scope

GRANT = "May edit: projects/proj promote.toml proj proj/promote.toml"


@pytest.mark.parametrize(
    ("layout", "path"),
    [
        ("workspace", "projects/proj/promote.toml"),
        ("sandbox", "proj/promote.toml"),
        ("root", "promote.toml"),
    ],
)
def test_the_project_manifest_blocks_in_every_layout_even_when_the_brief_grants_it(layout, path):
    assert _gate_scope.scope_violations([path], "proj", GRANT, None, layout) == [path]
    assert _gate_scope.scope_violations([path], "proj", "", None, layout) == [path]


def test_a_promote_toml_deeper_in_the_tree_is_the_targets_own_file():
    path = "projects/proj/docs/promote.toml"
    assert _gate_scope.scope_violations([path], "proj", "", None, "workspace") == []


def test_other_project_files_are_unaffected():
    assert _gate_scope.scope_violations(["projects/proj/a.py"], "proj", "", None, "workspace") == []
