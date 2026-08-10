#!/usr/bin/env python3
"""Verify a locally generated CodeKG Python ingestion stress corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MANIFEST_NAME = "codekg-stress-manifest.json"
GENERATOR_VERSION = 1
REQUIRED_CONFIG = {
    "packages",
    "modules_per_package",
    "classes_per_module",
    "functions_per_module",
    "methods_per_class",
    "syntax_error_modules",
    "large_modules",
    "large_module_bytes",
    "seed",
}
SCAN_SAFE_PRESETS = {"small", "medium"}


def _sha256(parts: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def _digest_strings(values: Iterable[str]) -> str:
    return _sha256(value.encode() + b"\n" for value in sorted(values))


def _source_digest(root: Path) -> str:
    """Hash corpus files in canonical path order without buffering file contents."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != MANIFEST_NAME:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"missing corpus manifest: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid corpus manifest JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("corpus manifest must contain an object")
    return manifest


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported or missing manifest schema_version")
    generator = manifest.get("generator")
    if not isinstance(generator, dict):
        raise ValueError("manifest generator must contain an object")
    if generator.get("name") != "codekg-stress-corpus":
        raise ValueError("manifest generator name is invalid")
    if generator.get("version") != GENERATOR_VERSION:
        raise ValueError("manifest generator version is unsupported")
    if not isinstance(generator.get("source_sha256"), str):
        raise ValueError("manifest generator source_sha256 is invalid")
    generator_path = Path(__file__).with_name("generate.py")
    current_generator_digest = hashlib.sha256(generator_path.read_bytes()).hexdigest()
    if generator["source_sha256"] != current_generator_digest:
        raise ValueError("manifest generator source_sha256 does not match this generator")
    config = manifest.get("expanded_config")
    if not isinstance(config, dict) or set(config) != REQUIRED_CONFIG:
        raise ValueError("manifest expanded_config has invalid keys")
    if not all(type(value) is int and value >= 0 for value in config.values()):
        raise ValueError("manifest expanded_config values must be non-negative integers")
    if not config["packages"] or not config["modules_per_package"]:
        raise ValueError("manifest packages and modules_per_package must be greater than zero")
    if config["syntax_error_modules"] > config["packages"] * config["modules_per_package"]:
        raise ValueError("manifest syntax_error_modules exceeds generated module count")
    if not isinstance(manifest.get("canonical_source_sha256"), str):
        raise ValueError("manifest canonical_source_sha256 is invalid")
    oracle = manifest.get("oracle")
    expected_oracle = {
        "files",
        "valid_python_files",
        "declared_syntax_errors",
        "observed_syntax_errors",
        "symbols",
        "calls",
        "python_file_path_sha256",
        "symbol_qname_sha256",
        "call_owner_qname_sha256",
    }
    if not isinstance(oracle, dict) or set(oracle) != expected_oracle:
        raise ValueError("manifest oracle has invalid keys")
    count_keys = expected_oracle - {
        "python_file_path_sha256",
        "symbol_qname_sha256",
        "call_owner_qname_sha256",
    }
    if not all(type(oracle[key]) is int and oracle[key] >= 0 for key in count_keys):
        raise ValueError("manifest oracle counts must be non-negative integers")
    digest_keys = {
        "python_file_path_sha256",
        "symbol_qname_sha256",
        "call_owner_qname_sha256",
    }
    if not all(isinstance(oracle[key], str) for key in digest_keys):
        raise ValueError("manifest oracle digests must be strings")
    return {key: int(value) for key, value in config.items()}


def _preset_name(config: dict[str, int]) -> str | None:
    presets_path = Path(__file__).with_name("presets.json")
    presets = json.loads(presets_path.read_text(encoding="utf-8"))
    for name, preset_config in presets.items():
        if preset_config == config:
            return str(name)
    return None


def _verify_syntax(root: Path, manifest: dict[str, Any]) -> None:
    config = manifest["expanded_config"]
    declared_bad = {
        f"invalid/syntax_error_{index:04d}.py"
        for index in range(config["syntax_error_modules"])
    }
    python_paths = sorted(root.rglob("*.py"))
    invalid: set[str] = set()
    for path in python_paths:
        relative = path.relative_to(root).as_posix()
        try:
            compile(path.read_bytes(), relative, "exec")
        except SyntaxError:
            invalid.add(relative)
    if invalid != declared_bad:
        raise ValueError(
            "syntax-error declarations do not match corpus files: "
            f"declared={sorted(declared_bad)}, observed={sorted(invalid)}"
        )
    oracle = manifest["oracle"]
    observed = {
        "files": len(python_paths),
        "valid_python_files": len(python_paths) - len(invalid),
        "declared_syntax_errors": len(declared_bad),
        "observed_syntax_errors": len(invalid),
        "python_file_path_sha256": _digest_strings(
            path.relative_to(root).as_posix() for path in python_paths
        ),
    }
    for key, value in observed.items():
        if oracle[key] != value:
            raise ValueError(
                f"syntax oracle mismatch for {key}: expected {oracle[key]!r}, got {value!r}"
            )


def _verify_scan(root: Path, manifest: dict[str, Any]) -> None:
    try:
        from codekg.ingest import scan_repository
    except ImportError as error:  # pragma: no cover - depends on invocation environment
        raise ValueError("CodeKG must be installed to run scanner verification") from error
    repository = scan_repository(root)
    oracle = manifest["oracle"]
    observed = {
        "files": len(repository.files),
        "symbols": sum(len(file.symbols) for file in repository.files),
        "calls": sum(len(file.calls) for file in repository.files),
        "observed_syntax_errors": sum(len(file.diagnostics) for file in repository.files),
        "symbol_qname_sha256": _digest_strings(
            symbol.qname for file in repository.files for symbol in file.symbols
        ),
        "call_owner_qname_sha256": _digest_strings(
            call.owner_qname for file in repository.files for call in file.calls
        ),
    }
    for key, value in observed.items():
        if oracle[key] != value:
            raise ValueError(
                f"scanner oracle mismatch for {key}: expected {oracle[key]!r}, got {value!r}"
            )


def verify(corpus: Path, *, allow_memory_heavy_verification: bool = False) -> dict[str, Any]:
    """Validate corpus identity and, when safe, its CodeKG scanner oracle."""
    root = corpus.resolve()
    if not root.is_dir():
        raise ValueError(f"Corpus path does not exist or is not a directory: {corpus}")
    manifest = _read_manifest(root)
    config = _validate_manifest(manifest)
    digest = _source_digest(root)
    if digest != manifest["canonical_source_sha256"]:
        raise ValueError(
            "canonical source digest mismatch; corpus files were modified, deleted, or added"
        )
    _verify_syntax(root, manifest)
    preset = _preset_name(config)
    scanner_verified = False
    if preset not in SCAN_SAFE_PRESETS and not allow_memory_heavy_verification:
        return {"ok": True, "preset": preset, "scanner_verified": scanner_verified}
    _verify_scan(root, manifest)
    scanner_verified = True
    return {"ok": True, "preset": preset, "scanner_verified": scanner_verified}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--allow-memory-heavy-verification", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(
            args.corpus,
            allow_memory_heavy_verification=args.allow_memory_heavy_verification,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
