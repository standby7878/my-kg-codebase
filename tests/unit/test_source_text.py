from __future__ import annotations

from pathlib import Path

import pytest

from codekg.source_text import extract_symbol_text

pytestmark = pytest.mark.unit


def test_extract_symbol_text_gets_docstring_and_filtered_comments(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "# coding: utf-8",
                "# Copyright 2024 Example",
                "# noqa: D401",
                "",
                "# Decide which standby should be promoted after failover.",
                "# Keep the rule deterministic across all observers.",
                "def do_failover_decision(nodes):",
                '    """Choose a node to promote."""',
                "    # return nodes[0]",
                "    # Ignore lagging members while selecting candidates.",
                "    return nodes[0]",
            ]
        ),
        encoding="utf-8",
    )

    result = extract_symbol_text(source, "sample.do_failover_decision", 8, 12)

    assert result.docstring == "Choose a node to promote."
    assert result.leading_comment is not None
    assert "standby should be promoted" in result.leading_comment
    assert "noqa" not in result.leading_comment
    assert "copyright" not in result.leading_comment.lower()
    assert result.inline_comments == ("Ignore lagging members while selecting candidates.",)
