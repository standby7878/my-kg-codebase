#!/usr/bin/env python3
"""Generate deterministic, local-only Python corpora for CodeKG stress tests."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MANIFEST_NAME = "codekg-stress-manifest.json"
GENERATOR_VERSION = 1
DEFAULT_OUTPUT_ROOT = Path(__file__).parents[2] / ".stress-corpora"
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


def _sha256(parts: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


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


def _digest_strings(values: Iterable[str]) -> str:
    return _sha256(value.encode() + b"\n" for value in sorted(values))


def _read_presets(path: Path) -> dict[str, dict[str, int]]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("presets JSON must contain an object")
    return {str(name): dict(value) for name, value in loaded.items()}


def _validate_config(config: dict[str, Any]) -> dict[str, int]:
    missing = REQUIRED_CONFIG - config.keys()
    unknown = config.keys() - REQUIRED_CONFIG
    if missing or unknown:
        raise ValueError(
            f"invalid config keys: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    expanded = {key: int(config[key]) for key in sorted(REQUIRED_CONFIG)}
    if any(value < 0 for value in expanded.values()):
        raise ValueError("all configuration values must be non-negative")
    if not expanded["packages"] or not expanded["modules_per_package"]:
        raise ValueError("packages and modules_per_package must be greater than zero")
    if expanded["syntax_error_modules"] > expanded["packages"] * expanded["modules_per_package"]:
        raise ValueError("syntax_error_modules exceeds generated module count")
    return expanded


def _module_source(
    package: int, module: int, config: dict[str, int]
) -> tuple[str, list[str], list[str]]:
    package_name = f"pkg_{package:04d}"
    module_name = f"module_{module:04d}"
    qprefix = f"{package_name}.{module_name}"
    seed_marker = hashlib.sha256(str(config["seed"]).encode()).hexdigest()[:12]
    lines = [
        f'"""Generated module {qprefix}; seed={seed_marker}."""',
        "from __future__ import annotations",
    ]
    if module:
        lines.append(f"from .module_{module - 1:04d} import Type_{package:04d}_{module - 1:04d}_0")
        parent = f"Type_{package:04d}_{module - 1:04d}_0"
    else:
        parent = "object"
    qnames: list[str] = []
    call_owners: list[str] = []
    for cls in range(config["classes_per_module"]):
        name = f"Type_{package:04d}_{module:04d}_{cls}"
        qname = f"{qprefix}.{name}"
        qnames.append(qname)
        lines.extend(("", f"class {name}({parent}):", f'    """Generated type {qname}."""'))
        for method in range(config["methods_per_class"]):
            method_name = f"method_{method:03d}"
            method_qname = f"{qname}.{method_name}"
            qnames.append(method_qname)
            lines.extend(
                (
                    f"    def {method_name}(self, value: int = {method}) -> int:",
                    f"        local = {name}()",
                    "        return local._identity(value)",
                    "",
                )
            )
            call_owners.extend((method_qname, method_qname))
        qnames.append(f"{qname}._identity")
        lines.extend(("    def _identity(self, value: int) -> int:", "        return value", ""))
    for function in range(config["functions_per_module"]):
        name = f"function_{function:03d}"
        qnames.append(f"{qprefix}.{name}")
        target = f"Type_{package:04d}_{module:04d}_0"
        lines.extend(
            (
                f"def {name}(value: int = {function}) -> int:",
                f"    local = {target}()",
                "    return local.method_000(value)",
                "",
            )
        )
        call_owners.extend((f"{qprefix}.{name}", f"{qprefix}.{name}"))
    return "\n".join(lines) + "\n", qnames, call_owners


def _syntax_error_source(index: int) -> str:
    return f"# Declared invalid fixture {index}\ndef deliberately_invalid_{index}(:\n    pass\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _validated_output(output: Path, output_root: Path) -> Path:
    """Return a safe output path contained by the explicitly selected corpus root."""
    if output_root.is_symlink():
        raise ValueError("refusing to use a symlink as a stress corpus root")
    root = output_root.resolve()
    candidate = output.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"output must be a child of stress corpus root: {root}")
    if output.is_symlink():
        raise ValueError("refusing to use a symlink as a corpus output directory")
    return candidate


def _is_complete_generated_corpus(output: Path) -> bool:
    """Confirm an existing output is this generator's intact prior result."""
    manifest_path = output / MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        generator = manifest["generator"]
        config = _validate_config(manifest["expanded_config"])
        expected_digest = manifest["canonical_source_sha256"]
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if (
        manifest.get("schema_version") != 1
        or not isinstance(generator, dict)
        or generator.get("name") != "codekg-stress-corpus"
        or generator.get("version") != GENERATOR_VERSION
        or generator.get("source_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        or not isinstance(expected_digest, str)
        or not config
    ):
        return False
    try:
        return _source_digest(output) == expected_digest
    except OSError:
        return False


def _prepare_output(output: Path, output_root: Path, overwrite: bool, estimate: int) -> Path:
    output = _validated_output(output, output_root)
    if output.exists() and any(output.iterdir()):
        if not overwrite:
            raise ValueError(
                "output exists and is non-empty; use --overwrite for a generated corpus"
            )
        if not _is_complete_generated_corpus(output):
            raise ValueError(
                "refusing to overwrite a directory without an intact generated manifest"
            )
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output).free
    required = max(128 * 1024 * 1024, estimate * 2)
    if free < required:
        raise OSError(f"insufficient free space: need {required} bytes, have {free}")
    return output


def _oracle(
    root: Path, qnames: list[str], call_owners: list[str], syntax_errors: int
) -> dict[str, Any]:
    valid = invalid = 0
    for path in root.rglob("*.py"):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            valid += 1
        except SyntaxError:
            invalid += 1
    return {
        "files": len(list(root.rglob("*.py"))),
        "valid_python_files": valid,
        "declared_syntax_errors": syntax_errors,
        "observed_syntax_errors": invalid,
        "symbols": len(qnames),
        "calls": len(call_owners),
        "python_file_path_sha256": _digest_strings(
            path.relative_to(root).as_posix() for path in root.rglob("*.py")
        ),
        "symbol_qname_sha256": _digest_strings(qnames),
        "call_owner_qname_sha256": _digest_strings(call_owners),
    }


def generate(
    output: Path,
    config: dict[str, Any],
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate a corpus and return its public manifest without external dependencies."""
    config = _validate_config(config)
    module_count = config["packages"] * config["modules_per_package"]
    estimate = module_count * 4096 + config["large_modules"] * config["large_module_bytes"]
    if dry_run:
        return {"dry_run": True, "expanded_config": config, "estimated_source_bytes": estimate}
    output = _prepare_output(output, output_root, overwrite, estimate)
    qnames: list[str] = []
    call_owners: list[str] = []
    for package in range(config["packages"]):
        package_dir = output / f"pkg_{package:04d}"
        _write(package_dir / "__init__.py", f'"""Generated package {package:04d}."""\n')
        for module in range(config["modules_per_package"]):
            source, module_qnames, module_call_owners = _module_source(package, module, config)
            _write(package_dir / f"module_{module:04d}.py", source)
            qnames.extend(module_qnames)
            call_owners.extend(module_call_owners)
    for index in range(config["syntax_error_modules"]):
        _write(output / "invalid" / f"syntax_error_{index:04d}.py", _syntax_error_source(index))
    for index in range(config["large_modules"]):
        payload = "# generated filler\n" * (config["large_module_bytes"] // 19 + 1)
        _write(
            output / "generated" / f"large_{index:04d}.py",
            '"""Large generated fixture."""\n' + payload,
        )
    _write(
        output / "README.md",
        "# CodeKG stress corpus\n\nGenerated deterministically for ingestion benchmarks.\n",
    )
    oracle = _oracle(output, qnames, call_owners, config["syntax_error_modules"])
    generator_source = Path(__file__).read_bytes()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generator": {
            "name": "codekg-stress-corpus",
            "version": GENERATOR_VERSION,
            "source_sha256": hashlib.sha256(generator_source).hexdigest(),
        },
        "expanded_config": config,
        "canonical_source_sha256": _source_digest(output),
        "oracle": oracle,
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output, delete=False) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, output / MANIFEST_NAME)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", default="small", help="preset name from --config JSON")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("presets.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="root containing generated corpora (default: repository .stress-corpora)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        presets = _read_presets(args.config)
        if args.preset not in presets:
            raise ValueError(
                f"unknown preset {args.preset!r}; choices: {', '.join(sorted(presets))}"
            )
        manifest = generate(
            args.output,
            presets[args.preset],
            output_root=args.output_root,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
