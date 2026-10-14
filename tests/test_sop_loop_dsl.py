"""SOP DSL tests for explicit ``loops:`` blocks."""

from __future__ import annotations

import pytest

from agt.sop.dsl import SOPError, parse_sop


def _spec(cycle: bool = True, max_iterations: int = 3) -> str:
    deps = "depends_on: [fix]" if cycle else ""
    return f"""
name: review-fix-loop
loops:
  - name: refine
    nodes: [review, fix]
    max_iterations: {max_iterations}
tasks:
  - name: review
    provider: claude
    input: Review code
    output: review-report
    {deps}
  - name: fix
    provider: claude
    input: Apply ${{review.review-report}}
    depends_on: [review]
    output: fix-result
"""


def test_valid_loop_parses() -> None:
    spec = parse_sop(_spec())
    assert spec.name == "review-fix-loop"
    assert len(spec.loops) == 1
    loop = spec.loops[0]
    assert loop.name == "refine"
    assert loop.nodes == ["review", "fix"]
    assert loop.max_iterations == 3
    assert loop.entry == "review"
    review = spec.node("review")
    fix = spec.node("fix")
    assert review.loop_name == "refine"
    assert fix.loop_name == "refine"


def test_cycle_inside_loop_allowed() -> None:
    # review -> fix -> review forms a cycle, but inside a declared loop.
    spec = parse_sop(_spec(cycle=True))
    assert spec.node("review").depends_on == ["fix"]
    assert spec.node("fix").depends_on == ["review"]


def test_cycle_outside_loop_rejected() -> None:
    yaml = """
name: bad-cycle
tasks:
  - name: a
    input: hi
    depends_on: [b]
  - name: b
    input: hi
    depends_on: [a]
"""
    with pytest.raises(SOPError, match="cycle outside a declared loop"):
        parse_sop(yaml)


def test_missing_max_iterations_rejected() -> None:
    yaml = """
name: bad-loop
tasks:
  - name: a
    input: hi
loops:
  - name: loop
    nodes: [a]
"""
    with pytest.raises(SOPError, match="max_iterations"):
        parse_sop(yaml)


def test_unknown_loop_node_rejected() -> None:
    yaml = """
name: bad-loop
tasks:
  - name: a
    input: hi
loops:
  - name: loop
    nodes: [a, missing]
    max_iterations: 2
"""
    with pytest.raises(SOPError, match="unknown task 'missing'"):
        parse_sop(yaml)


def test_duplicate_loop_node_rejected() -> None:
    yaml = """
name: bad-loop
tasks:
  - name: a
    input: hi
loops:
  - name: loop
    nodes: [a, a]
    max_iterations: 2
"""
    with pytest.raises(SOPError, match="duplicate node"):
        parse_sop(yaml)


def test_node_in_multiple_loops_rejected() -> None:
    yaml = """
name: bad-loop
tasks:
  - name: a
    input: hi
  - name: b
    input: hi
loops:
  - name: loop1
    nodes: [a]
    max_iterations: 2
  - name: loop2
    nodes: [a, b]
    max_iterations: 2
"""
    with pytest.raises(SOPError, match="belongs to more than one loop"):
        parse_sop(yaml)


def test_entry_not_in_nodes_rejected() -> None:
    yaml = """
name: bad-loop
tasks:
  - name: a
    input: hi
  - name: b
    input: hi
loops:
  - name: loop
    nodes: [a]
    entry: b
    max_iterations: 2
"""
    with pytest.raises(SOPError, match="entry 'b' is not in 'nodes'"):
        parse_sop(yaml)


def test_max_iterations_exceeds_cap_rejected() -> None:
    from agt.sop.dsl import SOP_MAX_ITERATIONS

    yaml = f"""
name: big-loop
tasks:
  - name: a
    input: hi
loops:
  - name: loop
    nodes: [a]
    max_iterations: {SOP_MAX_ITERATIONS + 1}
"""
    with pytest.raises(SOPError, match="exceeds"):
        parse_sop(yaml)


def test_non_integer_max_iterations_rejected() -> None:
    yaml = """
name: bad-loop
tasks:
  - name: a
    input: hi
loops:
  - name: loop
    nodes: [a]
    max_iterations: "five"
"""
    with pytest.raises(SOPError, match="max_iterations"):
        parse_sop(yaml)


def test_entry_defaults_to_first_node() -> None:
    yaml = """
name: entry-default
tasks:
  - name: second
    input: hi
    depends_on: [first]
  - name: first
    input: hi
loops:
  - name: loop
    nodes: [second, first]
    max_iterations: 2
"""
    spec = parse_sop(yaml)
    assert spec.loops[0].entry == "second"


def test_self_dependency_still_rejected() -> None:
    yaml = """
name: self-loop
tasks:
  - name: a
    input: hi
    depends_on: [a]
loops:
  - name: loop
    nodes: [a]
    max_iterations: 2
"""
    with pytest.raises(SOPError, match="depends on itself"):
        parse_sop(yaml)


def test_placeholder_in_loop_validates() -> None:
    # Placeholder from fix to review is fine because review is in depends_on.
    spec = parse_sop(_spec())
    assert spec.node("fix").placeholders() == ["review.review-report"]


def test_placeholder_to_undeclared_output_rejected() -> None:
    yaml = """
name: bad-ph
tasks:
  - name: review
    input: Review
    output: review-report
  - name: fix
    input: Apply ${review.wrong-name}
    depends_on: [review]
loops:
  - name: refine
    nodes: [review, fix]
    max_iterations: 2
"""
    with pytest.raises(SOPError, match="undeclared output"):
        parse_sop(yaml)


def test_empty_loops_section_allowed() -> None:
    yaml = """
name: no-loop
tasks:
  - name: a
    input: hi
loops: []
"""
    spec = parse_sop(yaml)
    assert spec.loops == []


def test_no_loops_section_allowed() -> None:
    yaml = """
name: no-loop
tasks:
  - name: a
    input: hi
"""
    spec = parse_sop(yaml)
    assert spec.loops == []
