from __future__ import annotations

import json
from pathlib import Path

import pytest

from codekg.evaluation import (
    CorpusSpec,
    EvaluationError,
    _evaluate_callsite_truth,
    _evaluate_lexical_queries,
    _source_content_digest,
    _validate_pin,
    load_manifest,
    run_evaluation,
    write_report,
)
from codekg.ir import RepositoryIR


def _manifest(*, corpora: list[dict[str, object]]) -> dict[str, object]:
    return {"version": 1, "corpora": corpora}


def _required_corpora() -> list[dict[str, object]]:
    return [
        {
            "id": "synthetic",
            "kind": "synthetic",
            "path": "synthetic",
            "required": True,
            "pin": {"content_sha256": "a" * 64},
        },
        {
            "id": "dogfood",
            "kind": "dogfood",
            "path": "dogfood",
            "required": True,
            "pin": {"git_commit": "b" * 40},
        },
    ]


def test_manifest_validates_required_pinned_corpora(tmp_path: Path) -> None:
    path = tmp_path / "corpora.json"
    path.write_text(json.dumps(_manifest(corpora=_required_corpora())), encoding="utf-8")

    manifest = load_manifest(path)

    assert [corpus.corpus_id for corpus in manifest.corpora] == ["synthetic", "dogfood"]
    assert manifest.corpora[0].pin["content_sha256"] == "a" * 64


@pytest.mark.parametrize(
    "corpora, message",
    [
        (
            [
                {
                    "id": "only",
                    "kind": "synthetic",
                    "path": "synthetic",
                    "required": True,
                    "pin": {"content_sha256": "a" * 64},
                }
            ],
            "dogfood",
        ),
        (
            [
                *_required_corpora(),
                {
                    "id": "synthetic",
                    "kind": "external",
                    "path_env": "CODEKG_EVAL_PATH",
                    "required": False,
                    "pin": {"git_commit_env": "CODEKG_EVAL_COMMIT"},
                },
            ],
            "unique",
        ),
        (
            [
                *_required_corpora(),
                {
                    "id": "bad-pin",
                    "kind": "external",
                    "path": "external",
                    "required": False,
                    "pin": {"git_commit": "a", "content_sha256": "b"},
                },
            ],
            "requires one",
        ),
        (
            [
                {
                    **_required_corpora()[0],
                    "pin": {"content_sha256": "not-a-hash"},
                },
                _required_corpora()[1],
            ],
            "invalid digest",
        ),
    ],
)
def test_manifest_rejects_incomplete_or_ambiguous_contract(
    tmp_path: Path,
    corpora: list[dict[str, object]],
    message: str,
) -> None:
    path = tmp_path / "corpora.json"
    path.write_text(json.dumps(_manifest(corpora=corpora)), encoding="utf-8")

    with pytest.raises(EvaluationError, match=message):
        load_manifest(path)


def test_content_pin_includes_relative_names_and_file_content(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    first = _source_content_digest(tmp_path)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.py").write_text("value = 1\n", encoding="utf-8")

    assert first != _source_content_digest(tmp_path)


def test_write_report_is_stably_sorted(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "report.json"

    write_report({"z": 1, "a": {"y": 2, "b": 3}}, output)

    assert output.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "b": 3,\n    "y": 2\n  },\n  "z": 1\n}\n'
    )


def test_dogfood_working_tree_policy_reports_identity_without_claiming_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("codekg.evaluation._git_commit", lambda _: "a" * 40)
    monkeypatch.setattr("codekg.evaluation._git_dirty", lambda _: True)
    monkeypatch.setattr("codekg.evaluation._source_content_digest", lambda _: "b" * 64)
    spec = CorpusSpec("dogfood", "dogfood", ".", None, True, {"working_tree": "report"}, None)

    identity = _validate_pin(spec, tmp_path, require_pins=True)

    assert identity == {
        "policy": "working_tree_report",
        "git_commit": "a" * 40,
        "dirty": True,
        "content_sha256": "b" * 64,
        "verified": False,
    }


def test_optional_external_can_report_unpinned_or_require_a_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEKG_EVAL_EXTERNAL_COMMIT", raising=False)
    monkeypatch.setattr("codekg.evaluation._git_commit", lambda _: "a" * 40)
    monkeypatch.setattr("codekg.evaluation._git_dirty", lambda _: False)
    monkeypatch.setattr("codekg.evaluation._source_content_digest", lambda _: "b" * 64)
    spec = CorpusSpec(
        "external",
        "external",
        None,
        "CODEKG_EVAL_EXTERNAL_PATH",
        False,
        {"git_commit_env": "CODEKG_EVAL_EXTERNAL_COMMIT"},
        None,
    )

    assert _validate_pin(spec, tmp_path, require_pins=False)["policy"] == "optional_unpinned"
    with pytest.raises(EvaluationError, match="requires pin environment"):
        _validate_pin(spec, tmp_path, require_pins=True)


def test_lexical_queries_collect_exactly_five_timing_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_search(query: str, **_: object) -> list[dict[str, object]]:
        calls.append(query)
        return [{"qname": "sample.target"}]

    monkeypatch.setattr("codekg.evaluation.search_symbols", fake_search)

    result = _evaluate_lexical_queries(
        [{"query": "target", "target_qname": "sample.target"}],
        repo="sample",
        zvec_path="/isolated/zvec",
        client=None,  # type: ignore[arg-type]
    )

    assert calls == ["target"] * 5
    assert result["queries"][0]["all_samples_matched"] is True
    assert len(result["queries"][0]["samples"]) == 5
    assert result["timing_ms"]["sample_count"] == 5
    assert result["timing_ms"]["p95"] >= result["timing_ms"]["p50"]


def test_later_preflight_pin_mismatch_causes_zero_index_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic = tmp_path / "synthetic"
    dogfood = tmp_path / "dogfood"
    synthetic.mkdir()
    dogfood.mkdir()
    (synthetic / "module.py").write_text("def target():\n    return None\n", encoding="utf-8")
    (dogfood / "module.py").write_text("def target():\n    return None\n", encoding="utf-8")
    manifest_path = tmp_path / "corpora.json"
    manifest_path.write_text(
        json.dumps(
            _manifest(
                corpora=[
                    {
                        "id": "synthetic",
                        "kind": "synthetic",
                        "path": "synthetic",
                        "required": True,
                        "pin": {"content_sha256": _source_content_digest(synthetic)},
                    },
                    {
                        "id": "dogfood",
                        "kind": "dogfood",
                        "path": "dogfood",
                        "required": True,
                        "pin": {"content_sha256": "f" * 64},
                    },
                ]
            )
        ),
        encoding="utf-8",
    )
    indexed: list[Path] = []
    monkeypatch.setattr(
        "codekg.evaluation.index_repository",
        lambda path, **_: indexed.append(Path(path)),
    )

    with pytest.raises(EvaluationError, match="dogfood.*pin mismatch"):
        run_evaluation(
            manifest_path,
            project_root=tmp_path,
            zvec_root=tmp_path / "zvec",
        )

    assert indexed == []
    assert not (tmp_path / "zvec").exists()


def test_callsite_truth_uses_full_source_locator_and_rejects_extras() -> None:
    class FakeClient:
        def execute_read(self, *_: object, **__: object) -> list[dict[str, object]]:
            return [
                {
                    "owner_qname": "sample.run",
                    "start_line": 5,
                    "start_column": 4,
                    "ordinal": 1,
                    "raw_callee": "target",
                    "status": "exact_local",
                    "target_qname": "sample.target",
                    "calls": True,
                    "exact_calls": True,
                    "resolves_to": True,
                },
                {
                    "owner_qname": "sample.run",
                    "start_line": 6,
                    "start_column": 4,
                    "ordinal": 2,
                    "raw_callee": "unexpected",
                    "status": "unresolved",
                    "target_qname": None,
                    "calls": False,
                    "exact_calls": False,
                    "resolves_to": False,
                },
            ]

    truth = {
        "call_sites": [
            {
                "owner_qname": "sample.run",
                "start_line": 5,
                "start_column": 4,
                "ordinal": 1,
                "raw_callee": "target",
                "status": "exact_local",
                "target_qname": "sample.target",
                "projection": "exact",
            }
        ]
    }

    result = _evaluate_callsite_truth(
        truth,
        RepositoryIR("sample", "commit", "/sample"),
        FakeClient(),  # type: ignore[arg-type]
    )

    assert result["ok"] is False
    assert result["missing"] == []
    assert result["mismatches"] == []
    assert result["extras"] == [
        {
            "owner_qname": "sample.run",
            "start_line": 6,
            "start_column": 4,
            "ordinal": 2,
            "raw_callee": "unexpected",
        }
    ]
