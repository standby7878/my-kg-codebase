from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from codekg.ingest import scan_repository

GENERATOR = Path(__file__).parents[2] / "tools" / "stress_corpus" / "generate.py"
SPEC = importlib.util.spec_from_file_location("stress_corpus_generator", GENERATOR)
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)

VERIFIER = Path(__file__).parents[2] / "tools" / "stress_corpus" / "verify.py"
VERIFY_SPEC = importlib.util.spec_from_file_location("stress_corpus_verifier", VERIFIER)
assert VERIFY_SPEC and VERIFY_SPEC.loader
verifier = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verifier)

SMALL = {
    "packages": 1,
    "modules_per_package": 2,
    "classes_per_module": 1,
    "functions_per_module": 2,
    "methods_per_class": 2,
    "syntax_error_modules": 1,
    "large_modules": 0,
    "large_module_bytes": 0,
    "seed": 1729,
}


def generate(
    output: Path, config: dict[str, int] = SMALL, **kwargs: object
) -> dict[str, object]:
    return generator.generate(output, config, output_root=output.parent, **kwargs)


@pytest.mark.unit
def test_generation_is_deterministic_and_matches_scan_oracle(tmp_path: Path) -> None:
    first = generate(tmp_path / "one")
    second = generate(tmp_path / "two")
    assert first["canonical_source_sha256"] == second["canonical_source_sha256"]
    assert first["oracle"] == second["oracle"]
    manifest = json.loads((tmp_path / "one" / generator.MANIFEST_NAME).read_text())
    assert manifest == first
    for path in (tmp_path / "one").rglob("*.py"):
        if "syntax_error" not in path.name:
            ast.parse(path.read_text(), filename=str(path))
    repository = scan_repository(tmp_path / "one")
    assert len(repository.files) == first["oracle"]["files"]
    assert sum(len(file.symbols) for file in repository.files) == first["oracle"]["symbols"]
    assert sum(len(file.calls) for file in repository.files) == first["oracle"]["calls"]
    assert (
        sum(len(file.diagnostics) for file in repository.files)
        == first["oracle"]["observed_syntax_errors"]
    )
    assert (
        generator._digest_strings(
            symbol.qname for file in repository.files for symbol in file.symbols
        )
        == first["oracle"]["symbol_qname_sha256"]
    )
    assert (
        generator._digest_strings(
            call.owner_qname for file in repository.files for call in file.calls
        )
        == first["oracle"]["call_owner_qname_sha256"]
    )


@pytest.mark.unit
def test_dry_run_does_not_create_output(tmp_path: Path) -> None:
    output = tmp_path / "not-created"
    result = generate(output, dry_run=True)
    assert result["dry_run"] is True
    assert not output.exists()


@pytest.mark.unit
def test_refuses_non_generated_non_empty_output(tmp_path: Path) -> None:
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "keep.txt").write_text("do not delete")
    with pytest.raises(ValueError, match="non-empty"):
        generate(output)
    with pytest.raises(ValueError, match="intact generated manifest"):
        generate(output, overwrite=True)
    assert (output / "keep.txt").read_text() == "do not delete"


@pytest.mark.unit
def test_verifier_checks_generated_corpus_and_scanner_oracle(tmp_path: Path) -> None:
    output = tmp_path / "small"
    preset_config = json.loads(
        (GENERATOR.with_name("presets.json")).read_text(encoding="utf-8")
    )["small"]
    generate(output, preset_config)

    assert verifier.verify(output) == {
        "ok": True,
        "preset": "small",
        "scanner_verified": True,
    }


@pytest.mark.unit
def test_verifier_detects_added_deleted_and_modified_sources(tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    generate(output)
    (output / "added.py").write_text("x = 1\n")
    with pytest.raises(ValueError, match="modified, deleted, or added"):
        verifier.verify(output)

    (output / "added.py").unlink()
    (output / "pkg_0000" / "module_0000.py").unlink()
    with pytest.raises(ValueError, match="modified, deleted, or added"):
        verifier.verify(output)


@pytest.mark.unit
def test_verifier_checks_declared_syntax_errors(tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    generate(output)
    invalid = output / "invalid" / "syntax_error_0000.py"
    invalid.write_text("valid = True\n")
    manifest = json.loads((output / generator.MANIFEST_NAME).read_text())
    manifest["canonical_source_sha256"] = verifier._source_digest(output)
    (output / generator.MANIFEST_NAME).write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="syntax-error declarations"):
        verifier.verify(output)


@pytest.mark.unit
def test_verifier_skips_memory_heavy_scanner_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "corpus"
    generate(output)
    monkeypatch.setattr(verifier, "_preset_name", lambda config: "large")
    monkeypatch.setattr(
        verifier,
        "_verify_scan",
        lambda root, manifest: pytest.fail("memory-heavy scanner verification ran"),
    )

    assert verifier.verify(output) == {
        "ok": True,
        "preset": "large",
        "scanner_verified": False,
    }


@pytest.mark.unit
def test_generator_refuses_fake_manifest_before_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    output.mkdir()
    (output / "keep.txt").write_text("do not delete")
    (output / generator.MANIFEST_NAME).write_text("{}")

    with pytest.raises(ValueError, match="intact generated manifest"):
        generate(output, overwrite=True)
    assert (output / "keep.txt").read_text() == "do not delete"


@pytest.mark.unit
def test_generator_rejects_output_outside_selected_root_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="child of stress corpus root"):
        generator.generate(tmp_path / "outside-default-root", SMALL)

    with pytest.raises(ValueError, match="child of stress corpus root"):
        generator.generate(tmp_path / "outside", SMALL, output_root=tmp_path / "corpora")

    root = tmp_path / "corpora"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    output = root / "link"
    output.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        generator.generate(output, SMALL, output_root=root)

    linked_root = tmp_path / "linked-corpora"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink as a stress corpus root"):
        generator.generate(linked_root / "corpus", SMALL, output_root=linked_root)


@pytest.mark.unit
def test_seed_changes_canonical_source_digest(tmp_path: Path) -> None:
    changed_seed = {**SMALL, "seed": SMALL["seed"] + 1}
    first = generate(tmp_path / "one")
    second = generate(tmp_path / "two", changed_seed)
    assert first["canonical_source_sha256"] != second["canonical_source_sha256"]
