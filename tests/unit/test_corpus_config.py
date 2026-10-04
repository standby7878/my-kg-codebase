from __future__ import annotations

import pytest

from codekg.corpus_config import load_corpus_config


def test_manifest_resolves_explicit_dependencies(tmp_path):
    (tmp_path / "pg").mkdir()
    (tmp_path / "ext").mkdir()
    path = tmp_path / "corpus.toml"
    path.write_text(
        '[[snapshots]]\nalias="pg18"\nlogical_repo="postgres"\nversion="18"\nrole="postgres"\npath="pg"\n\n[[snapshots]]\nalias="cron18"\nlogical_repo="cron"\nversion="1"\nrole="extension"\npath="ext"\ndependencies=["pg18"]\n'
    )
    assert load_corpus_config(path).dependency_closure("cron18") == ("pg18",)


@pytest.mark.parametrize("alias", ["../escape", "", "a/b"])
def test_manifest_rejects_invalid_alias(tmp_path, alias):
    (tmp_path / "src").mkdir()
    path = tmp_path / "bad.toml"
    path.write_text(
        f'[[snapshots]]\nalias="{alias}"\nlogical_repo="repo"\nversion="1"\nrole="postgres"\npath="src"\n'
    )
    with pytest.raises(ValueError):
        load_corpus_config(path)


def test_manifest_rejects_dependency_cycles(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    path = tmp_path / "cycle.toml"
    path.write_text(
        '[[snapshots]]\nalias="a"\nlogical_repo="a"\nversion="1"\nrole="application"\npath="a"\ndependencies=["b"]\n\n[[snapshots]]\nalias="b"\nlogical_repo="b"\nversion="1"\nrole="application"\npath="b"\ndependencies=["a"]\n'
    )
    with pytest.raises(ValueError, match="cycle"):
        load_corpus_config(path)


def test_manifest_accepts_absolute_and_relative_parent_source_roots(tmp_path):
    workspace = tmp_path / "workspace"
    manifest_dir = workspace / "manifests"
    manifest_dir.mkdir(parents=True)
    external = workspace / "postgres"
    extension = workspace / "pg_cron"
    application = workspace / "application"
    external.mkdir()
    extension.mkdir()
    application.mkdir()
    path = manifest_dir / "corpus.toml"
    path.write_text(
        f'[[snapshots]]\nalias="pg"\nlogical_repo="postgres"\nversion="18"\n'
        f'role="postgres"\npath="{external}"\n\n'
        '[[snapshots]]\nalias="cron"\nlogical_repo="pg_cron"\nversion="1"\n'
        'role="extension"\npath="../pg_cron"\ndependencies=["pg"]\n\n'
        '[[snapshots]]\nalias="app"\nlogical_repo="application"\nversion="1"\n'
        f'role="application"\npath="{application}"\ndependencies=["pg"]\n'
    )
    corpus = load_corpus_config(path)
    assert corpus.by_alias["pg"].path == external
    assert corpus.by_alias["cron"].path == extension
    assert corpus.by_alias["app"].dependencies == ("pg",)


def test_dependency_closure_rejects_two_aliases_of_same_postgres_repo(tmp_path):
    for name in ("pg-a", "pg-b", "ext", "app"):
        (tmp_path / name).mkdir()
    path = tmp_path / "corpus.toml"
    path.write_text(
        '[[snapshots]]\nalias="pga"\nlogical_repo="postgres"\nversion="18"\n'
        'role="postgres"\npath="pg-a"\n'
        '[[snapshots]]\nalias="pgb"\nlogical_repo="postgres"\nversion="18"\n'
        'role="postgres"\npath="pg-b"\n'
        '[[snapshots]]\nalias="ext"\nlogical_repo="ext"\nversion="1"\n'
        'role="extension"\npath="ext"\ndependencies=["pga"]\n'
        '[[snapshots]]\nalias="app"\nlogical_repo="app"\nversion="1"\n'
        'role="application"\npath="app"\ndependencies=["ext", "pgb"]\n'
    )
    with pytest.raises(ValueError, match="multiple aliases"):
        load_corpus_config(path)
