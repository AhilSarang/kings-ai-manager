"""Every command pattern, audited for the one mistake that keeps getting made.

Twice now a command has quietly swallowed an instruction meant for the agent, both times for the
same reason: an ungrouped top-level alternation. The command decorator wraps each pattern as

    ^\\s*{pattern}\\s*[.!]?\\s*$

and `|` binds looser than concatenation, so `r"ppc|ads"` compiles to `^\\s*ppc` OR `ads\\s*[.!]?\\s*$`
— the first branch anchored only at the start, the second only at the end. "ppc please cut the bids
on skyzz" then matched the first branch, and a real instruction was answered with a status summary.

The original fix was per-command, and the second occurrence was in code I wrote after making it. So
this test is structural instead: it reads every registered pattern and fails if any alternation sits
outside a group, whoever adds it and whenever. Wrapping the whole thing in `(?: … )` is the fix.
"""

import pytest

from app.services import commands


def top_level_alternation(pattern: str) -> bool:
    """True when `pattern` has a `|` outside every group and character class.

    Hand-written rather than taken from `re`'s parser, because `re` exposes no stable way to ask
    this and the scan itself is four lines: track paren depth, skip character classes, honour
    backslash escapes.
    """
    depth = 0
    in_class = False
    class_is_empty = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            # A `]` immediately after `[` or `[^` is a literal, not the end of the class — `[]]` is
            # a class containing one bracket. Tracked so the scanner cannot be fooled into thinking
            # a class ended early and then reading a `|` inside it as a top-level alternation.
            if char == "]" and not class_is_empty:
                in_class = False
            class_is_empty = False
        elif char == "[":
            in_class = True
            class_is_empty = True
            if pattern[index + 1:index + 2] == "^":
                index += 1              # `[^]]` — the caret does not make the class non-empty
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            return True
        index += 1
    return False


def test_the_scanner_itself_works():
    assert top_level_alternation("ppc|ads") is True
    assert top_level_alternation("(?:ppc|ads)") is False
    assert top_level_alternation(r"a\|b") is False           # an escaped pipe is a literal
    assert top_level_alternation("[a|b]") is False           # inside a class it is a literal too
    assert top_level_alternation("(?:a)|(?:b)") is True
    assert top_level_alternation(r"\\|x") is True            # escaped backslash, then a real pipe
    assert top_level_alternation("(a[)]|b)") is False        # a bracketed paren does not end a group
    assert top_level_alternation("[]|]") is False            # `]` right after `[` is a literal
    assert top_level_alternation("[^]|]") is False           # and after `[^` too
    assert top_level_alternation("[]]|x") is True            # the class ends, then a real pipe


@pytest.mark.parametrize("command", commands.COMMANDS, ids=lambda c: c.name)
def test_no_command_pattern_alternates_outside_a_group(command):
    source = command.pattern.pattern
    assert not top_level_alternation(source), (
        f"{command.name} has a top-level `|`, so the anchors bind to one alternative each and it "
        f"will match text with words after it. Wrap the whole pattern in (?: … ).\n  {source}")


def test_the_decorator_anchors_what_it_is_given():
    """The anchors the whole audit above depends on.

    Not parametrized over every command: the decorator adds `^` and `$` to all of them, so asking
    each one whether it starts with `^` was a test of the decorator dressed up as 1,090 lines of
    coverage, and it could not have failed. This asserts the wrapper once, where it lives, and the
    behavioural consequence beside it.
    """
    source = commands.COMMANDS[0].pattern.pattern
    assert source.startswith("^\\s*") and source.endswith("\\s*[.!]?\\s*$"), source
    # And the consequence: a grouped pattern really does refuse trailing words.
    import re

    grouped = re.compile(r"^\s*(?:ppc\s+)?alerts?\s*[.!]?\s*$", re.I)
    assert grouped.match("ppc alerts")
    assert grouped.match("alerts.")
    assert not grouped.match("alerts please pause everything")


def test_a_command_word_with_an_instruction_after_it_reaches_the_agent():
    """The two cases that actually happened, kept as the regression they are."""
    for instruction in ("ppc please cut the bids on skyzz",
                        "alerts please pause the skyzz campaign",
                        "budgets are too high can you cut them",
                        "ppc hours of operation for the warehouse"):
        assert commands.match(instruction) is None, (
            f"{instruction!r} is for the agent to read, not a command to answer with a summary")
