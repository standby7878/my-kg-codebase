#!/usr/bin/env bash
# Replace the local dev-local graph from explicit, local Git checkouts.
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
compose="$root/compose/dev-local/docker-compose.yml"
env_file="$root/compose/dev-local/env"
runtime_file="$root/compose/dev-local/runtime.env"

usage() {
    cat <<'HELP'
Usage: rebuild-kg.sh --application ALIAS PATH REF --postgres ALIAS PATH REF
                     [--postgres ALIAS PATH REF ...]
                     [--extension ALIAS PATH REF ...] [--workers N]
                     [--skip-build] (--dry-run | --yes)

REF is a locally available Git ref, or WORKTREE for the current checkout
(including uncommitted files). This replaces the dev-local Compose graph,
search index, and staging volumes. It does not fetch or alter input checkouts.
HELP
}

die() { echo "rebuild-kg: $*" >&2; exit 1; }
toml_string() {
    local value="$1"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] || die 'tabs and newlines in paths/refs are unsupported'
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

declare -a roles=() aliases=() paths=() refs=() commits=() mounts=()
declare -A used=()
workers=1
yes=0
dry_run=0
skip_build=0

add_repo() {
    local role="$1" alias="$2" path="$3" ref="$4" resolved commit
    [[ "$alias" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || die "invalid alias: $alias"
    [[ -z "${used[$alias]+x}" ]] || die "duplicate alias: $alias"
    [[ -d "$path" ]] || die "repository directory not found: $path"
    resolved="$(cd -- "$path" && pwd -P)"
    git -C "$resolved" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a Git checkout: $path"
    if [[ "$ref" == WORKTREE ]]; then
        commit="$(git -C "$resolved" rev-parse --verify HEAD)" || die "no HEAD in $path"
    else
        commit="$(git -C "$resolved" rev-parse --verify --quiet --end-of-options "${ref}^{commit}")" || die "unknown local ref $ref in $path"
    fi
    used[$alias]=1
    roles+=("$role") aliases+=("$alias") paths+=("$resolved") refs+=("$ref") commits+=("$commit")
}

while (($#)); do
    case "$1" in
        --application|--postgres|--extension)
            (($# >= 4)) || die "$1 requires ALIAS PATH REF"
            add_repo "${1#--}" "$2" "$3" "$4"
            shift 4 ;;
        --workers)
            (($# >= 2)) || die '--workers requires a positive integer'
            workers="$2"; shift 2 ;;
        --workers=*) workers="${1#*=}"; shift ;;
        --yes) yes=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        --skip-build) skip_build=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done
[[ "$workers" =~ ^[1-9][0-9]*$ ]] || die '--workers must be a positive integer'
((yes + dry_run == 1)) || die 'choose exactly one of --dry-run and --yes'
((${#aliases[@]})) || die 'provide at least one repository'
pg_count=0
app_count=0
for role in "${roles[@]}"; do
    [[ "$role" != postgres ]] || ((pg_count+=1))
    [[ "$role" != application ]] || ((app_count+=1))
done
((pg_count && app_count)) || die 'provide at least one --application and --postgres'

# Validate aliases that will be synthesized for each PostgreSQL context before
# crossing the destructive Compose boundary.
declare -A snapshot_aliases=()
for ((i=0; i<${#aliases[@]}; i++)); do
    [[ "${roles[i]}" != postgres ]] || snapshot_aliases[${aliases[i]}]=1
done
for ((p=0; p<${#aliases[@]}; p++)); do
    [[ "${roles[p]}" == postgres ]] || continue
    for ((i=0; i<${#aliases[@]}; i++)); do
        [[ "${roles[i]}" == postgres ]] && continue
        context="${aliases[i]}-${aliases[p]}"
        ((${#context} <= 64)) || die "generated alias is too long: $context"
        [[ -z "${snapshot_aliases[$context]+x}" ]] || die "generated alias collision: $context"
        snapshot_aliases[$context]=1
    done
done

echo 'Rebuild plan (dev-local Compose only):'
for ((i=0; i<${#aliases[@]}; i++)); do
    printf '  %s %s: %s @ %s (%s)\n' "${roles[i]}" "${aliases[i]}" "${paths[i]}" "${refs[i]}" "${commits[i]}"
done
echo '  Replace dev-local containers and their graph, search, logs, and staging volumes.'
((dry_run == 0)) || exit 0

# Worktrees and the generated manifest are retained: corpus provenance points
# at source paths. No input checkout is switched to a different branch.
state="$(mktemp -d "${TMPDIR:-/var/tmp}/codekg-rebuild.XXXXXXXX")"
mkdir -p "$state/worktrees"
# The exporter image runs as UID 10001, not as the invoking host user.
chmod 0755 "$state" "$state/worktrees"
umask 022
config="$state/corpus.toml"
for ((i=0; i<${#aliases[@]}; i++)); do
    source="${paths[i]}"
    if [[ "${refs[i]}" != WORKTREE ]]; then
        source="$state/worktrees/${aliases[i]}"
        git -C "${paths[i]}" worktree add --detach "$source" "${commits[i]}"
    fi
    mounts+=(-v "$source:/repos/${aliases[i]}:ro")
done

snapshot() {
    local alias="$1" logical="$2" version="$3" role="$4" source_alias="$5" deps="$6"
    {
        echo '[[snapshots]]'
        printf 'alias = '; toml_string "$alias"; echo
        printf 'logical_repo = '; toml_string "$logical"; echo
        printf 'version = '; toml_string "$version"; echo
        printf 'role = '; toml_string "$role"; echo
        printf 'path = '; toml_string "/repos/$source_alias"; echo
        [[ -z "$deps" ]] || printf 'dependencies = [%s]\n' "$deps"
        if [[ "$role" == application ]]; then
            echo '[snapshots.sql]'
            echo 'enabled = true'
            echo 'include = ["**/*.sql", "**/*.sql.in"]'
        fi
        echo
    } >> "$config"
}

for ((p=0; p<${#aliases[@]}; p++)); do
    [[ "${roles[p]}" == postgres ]] || continue
    snapshot "${aliases[p]}" postgres "${refs[p]}@${commits[p]}" postgres "${aliases[p]}" ''
done
for ((p=0; p<${#aliases[@]}; p++)); do
    [[ "${roles[p]}" == postgres ]] || continue
    pg_alias="${aliases[p]}"
    dependencies="$(toml_string "$pg_alias")"
    for ((i=0; i<${#aliases[@]}; i++)); do
        [[ "${roles[i]}" == extension ]] || continue
        context="${aliases[i]}-$pg_alias"
        snapshot "$context" "${aliases[i]}" "${refs[i]}@${commits[i]}" extension "${aliases[i]}" "$(toml_string "$pg_alias")"
        dependencies+=", $(toml_string "$context")"
    done
    for ((i=0; i<${#aliases[@]}; i++)); do
        [[ "${roles[i]}" == application ]] || continue
        snapshot "${aliases[i]}-$pg_alias" "${aliases[i]}" "${refs[i]}@${commits[i]}" application "${aliases[i]}" "$dependencies"
    done
done
chmod 0644 "$config"

args=(-f "$compose" --env-file "$env_file")
[[ ! -f "$runtime_file" ]] || args+=(--env-file "$runtime_file")
dc() { docker compose "${args[@]}" "$@"; }

echo "Generated corpus config: $config"
if ((skip_build == 0)); then
    dc build app-image-build bulk-importer
fi
echo 'Stopping and deleting the existing dev-local Compose stack and volumes.'
dc down --volumes --remove-orphans
dc run --rm --no-deps -v "$state:/inputs:ro" "${mounts[@]}" bulk-exporter \
    codekg bulk-export-corpus /inputs/corpus.toml /data/bulk --workers "$workers"
dc run --rm --no-deps bulk-exporter codekg bulk-zvec /data/bulk/manifest.json
dc run --rm --no-deps bulk-importer
dc up -d --wait neo4j
dc run --rm --no-deps schema_bootstrap
dc up -d mcp
echo "Rebuild complete. Neo4j and MCP are running. Source worktrees: $state"
