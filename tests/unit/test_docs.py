from __future__ import annotations

from pathlib import Path

import pytest

from codekg.docs import chunk_docs, extract_mentions, resolve_markdown_descriptions

pytestmark = pytest.mark.unit


def test_chunk_docs_splits_markdown_and_extracts_mentions(tmp_path: Path) -> None:
    doc = tmp_path / "spec.md"
    doc.write_text(
        "\n".join(
            [
                "# Failover",
                "Call `patroni.ha.Ha.do_failover_decision` when promotion is needed.",
                "",
                "## Watchdog",
                "The ``Failsafe.update`` path refreshes state.",
            ]
        ),
        encoding="utf-8",
    )

    chunks = list(chunk_docs([tmp_path], ["**/*.md", "*.md"]))

    assert [chunk.heading_path for chunk in chunks] == ["Failover", "Failover > Watchdog"]
    assert "patroni.ha.Ha.do_failover_decision" in chunks[0].mentions
    assert "Failsafe.update" in chunks[1].mentions


def test_chunk_docs_does_not_extract_rst(tmp_path: Path) -> None:
    doc = tmp_path / "README.rst"
    doc.write_text(
        "\n".join(
            [
                "PGHoard",
                "=======",
                "",
                "Backup tool using ``pghoard.archive_sync.ArchiveSync``.",
                "",
                "Restore",
                "-------",
                "Restore WAL files.",
            ]
        ),
        encoding="utf-8",
    )

    chunks = list(chunk_docs([doc], ["*.rst"]))

    assert chunks == []


def test_extract_mentions_filters_plain_prose() -> None:
    assert extract_mentions("plain text without symbols") == ()
    assert extract_mentions("Use `pkg.mod.func` and ``Class.method``") == (
        "pkg.mod.func",
        "Class.method",
    )


def test_resolve_markdown_descriptions_requires_exact_callable_qname(tmp_path: Path) -> None:
    doc = tmp_path / "spec.md"
    doc.write_text(
        "\n".join(
            [
                "# Failover",
                "Use `patroni.ha.Ha.promote` to promote a standby.",
                "`Ha.promote` and `promote` are deliberately ambiguous.",
                "```python",
                "patroni.ha.Ha.restart",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    descriptions = resolve_markdown_descriptions(
        [doc], {"patroni.ha.Ha.promote", "patroni.ha.Ha.restart"}
    )

    assert descriptions == {
        "patroni.ha.Ha.promote": (
            "# Failover\n"
            "Use `patroni.ha.Ha.promote` to promote a standby.\n"
            "`Ha.promote` and `promote` are deliberately ambiguous.",
        ),
        "patroni.ha.Ha.restart": ("```python\npatroni.ha.Ha.restart\n```",),
    }
