#!/bin/bash

set -euo pipefail

show_usage() {
    echo "Usage: $0 <PROFILE> [COMMAND ...]"
    echo ""
    echo "Profiles:"
    echo "  dev-local"
    echo ""
    echo "Commands:"
    echo "  build          Build images"
    echo "  start          Start Neo4j, bootstrap schema, and MCP"
    echo "  stop           Stop services"
    echo "  restart        Restart services"
    echo "  logs           Follow logs"
    echo "  status         Show service status"
    echo "  clean          Stop and remove volumes/images"
    echo "  bootstrap      Run schema bootstrap"
    echo "  index-sources  Bulk-index every repository under CODEKG_REPOS_ROOT"
    echo "  help           Show this help"
}

if [ "${1:-}" = "" ]; then
    show_usage
    exit 1
fi

PROFILE="$1"
PROFILE_DIR="compose/${PROFILE}"
COMPOSE_FILE="${PROFILE_DIR}/docker-compose.yml"
ENV_FILE="${PROFILE_DIR}/env"
RUNTIME_ENV_FILE="${CODEKG_RUNTIME_ENV_FILE:-${PROFILE_DIR}/runtime.env}"

if [ "$PROFILE" != "dev-local" ]; then
    echo "Unknown profile: ${PROFILE}"
    show_usage
    exit 1
fi

if [ ! -f "$COMPOSE_FILE" ] || [ ! -f "$ENV_FILE" ]; then
    echo "Missing compose profile files under ${PROFILE_DIR}"
    exit 1
fi

shift || true
if [ "$#" -eq 0 ]; then
    set -- start
fi

INDEX_MODE="${CODEKG_INGEST_MODE:-auto}"
declare -a COMMANDS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode)
            if [ "$#" -lt 2 ]; then
                echo "--mode requires auto, bulk, or transactional" >&2
                exit 1
            fi
            INDEX_MODE="$2"
            shift 2
            ;;
        --mode=*)
            INDEX_MODE="${1#--mode=}"
            shift
            ;;
        *)
            COMMANDS+=("$1")
            shift
            ;;
    esac
done

if [ "${#COMMANDS[@]}" -eq 0 ]; then
    set -- start
else
    set -- "${COMMANDS[@]}"
fi

dc() {
    local -a arguments=(-f "$COMPOSE_FILE" --env-file "$ENV_FILE")
    if [ -f "$RUNTIME_ENV_FILE" ]; then
        arguments+=(--env-file "$RUNTIME_ENV_FILE")
    fi
    docker compose "${arguments[@]}" "$@"
}

repos_root_from_config() {
    if [[ -v CODEKG_REPOS_ROOT ]]; then
        printf '%s' "$CODEKG_REPOS_ROOT"
        return
    fi

    while IFS= read -r line; do
        case "$line" in
            CODEKG_REPOS_ROOT=*)
                printf '%s' "${line#CODEKG_REPOS_ROOT=}"
                return
                ;;
        esac
    done < "$ENV_FILE"

    echo "CODEKG_REPOS_ROOT is not set in the environment or ${ENV_FILE}" >&2
    return 1
}

resolve_repositories() {
    local configured entry candidate resolved basename remaining has_more
    repositories=()
    declare -gA resolved_paths=() basenames=()

    configured="$(repos_root_from_config)"
    remaining="$configured"
    while :; do
        if [[ "$remaining" == *';'* ]]; then
            entry="${remaining%%;*}"
            remaining="${remaining#*;}"
            has_more=1
        else
            entry="$remaining"
            remaining=''
            has_more=0
        fi

        if [[ -z "$entry" ]]; then
            echo "CODEKG_REPOS_ROOT contains an empty repository path" >&2
            return 1
        fi

        if [[ "$entry" = /* ]]; then
            candidate="$entry"
        else
            candidate="$PROFILE_DIR/$entry"
        fi
        if ! resolved="$(cd -- "$candidate" 2>/dev/null && pwd -P)"; then
            echo "Repository path does not resolve to an existing directory: ${entry}" >&2
            return 1
        fi

        if [[ -n "${resolved_paths[$resolved]+x}" ]]; then
            echo "Duplicate repository path: ${resolved}" >&2
            return 1
        fi
        basename="${resolved##*/}"
        if [[ -z "$basename" || -n "${basenames[$basename]+x}" ]]; then
            echo "Duplicate checkout basename: ${basename}" >&2
            return 1
        fi

        resolved_paths["$resolved"]=1
        basenames["$basename"]=1
        repositories+=("$resolved")

        [[ "$has_more" -eq 0 ]] && break
    done

}

transactional_index_sources() {
    local resolved basename
    for resolved in "${repositories[@]}"; do
        basename="${resolved##*/}"
        dc run --rm -v "$resolved:/repos/$basename:ro" ingestion codekg reindex "/repos/$basename"
    done
}

write_runtime_generation() {
    local graph_volume="$1" zvec_volume="$2" logs_volume="$3" temporary
    temporary="$(mktemp "${PROFILE_DIR}/.runtime.XXXXXX")"
    {
        printf 'CODEKG_NEO4J_DATA_VOLUME=%s\n' "$graph_volume"
        printf 'CODEKG_ZVEC_DATA_VOLUME=%s\n' "$zvec_volume"
        printf 'CODEKG_NEO4J_LOGS_VOLUME=%s\n' "$logs_volume"
    } > "$temporary"
    mv "$temporary" "$RUNTIME_ENV_FILE"
}

restore_runtime_generation() {
    local backup="$1"
    if [ -n "$backup" ] && [ -f "$backup" ]; then
        mv "$backup" "$RUNTIME_ENV_FILE"
    else
        rm -f "$RUNTIME_ENV_FILE"
    fi
}

bulk_index_sources() {
    local generation graph_volume zvec_volume logs_volume staging_volume backup_runtime
    local resolved basename
    local -a mounts=() repository_paths=()

    generation="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    graph_volume="codekg-dev-local_neo4j_data_${generation}"
    zvec_volume="codekg-dev-local_zvec_data_${generation}"
    logs_volume="codekg-dev-local_neo4j_logs_${generation}"
    staging_volume="codekg-dev-local_bulk_staging_${generation}"
    backup_runtime=""

    for resolved in "${repositories[@]}"; do
        basename="${resolved##*/}"
        mounts+=(-v "$resolved:/repos/$basename:ro")
        repository_paths+=("/repos/$basename")
    done

    CODEKG_BULK_STAGING_VOLUME="$staging_volume" \
        CODEKG_ZVEC_DATA_VOLUME="$zvec_volume" \
        dc run --rm --no-deps "${mounts[@]}" bulk-exporter \
        codekg bulk-export /data/bulk "${repository_paths[@]}"
    CODEKG_BULK_STAGING_VOLUME="$staging_volume" \
        CODEKG_ZVEC_DATA_VOLUME="$zvec_volume" \
        dc run --rm --no-deps "${mounts[@]}" bulk-exporter \
        codekg bulk-zvec "${repository_paths[@]}"
    CODEKG_BULK_STAGING_VOLUME="$staging_volume" \
        CODEKG_NEO4J_DATA_VOLUME="$graph_volume" \
        CODEKG_NEO4J_LOGS_VOLUME="$logs_volume" \
        dc run --rm --no-deps bulk-importer

    if [ -f "$RUNTIME_ENV_FILE" ]; then
        backup_runtime="$(mktemp "${PROFILE_DIR}/.runtime-backup.XXXXXX")"
        cp "$RUNTIME_ENV_FILE" "$backup_runtime"
    fi

    dc stop mcp neo4j
    write_runtime_generation "$graph_volume" "$zvec_volume" "$logs_volume"

    if ! dc up -d --wait neo4j; then
        restore_runtime_generation "$backup_runtime"
        dc up -d neo4j schema_bootstrap mcp
        return 1
    fi
    if ! dc run --rm --no-deps schema_bootstrap; then
        dc stop neo4j
        restore_runtime_generation "$backup_runtime"
        dc up -d neo4j schema_bootstrap mcp
        return 1
    fi
    if ! CODEKG_BULK_STAGING_VOLUME="$staging_volume" \
        dc run --rm --no-deps "${mounts[@]}" bulk-exporter \
        codekg validate-bulk-index "${repository_paths[@]}"; then
        dc stop neo4j
        restore_runtime_generation "$backup_runtime"
        dc up -d neo4j schema_bootstrap mcp
        return 1
    fi
    rm -f "$backup_runtime"
    dc up -d mcp
    echo "Published bulk generation ${generation}"
}

index_sources() {
    resolve_repositories
    case "$INDEX_MODE" in
        auto|bulk)
            bulk_index_sources
            ;;
        transactional)
            transactional_index_sources
            ;;
        *)
            echo "Unknown ingestion mode: ${INDEX_MODE}. Use auto, bulk, or transactional." >&2
            return 1
            ;;
    esac
}

for command in "$@"; do
    case "$command" in
        build)
            dc build app-image-build
            dc build
            ;;
        start)
            dc up -d neo4j schema_bootstrap mcp
            ;;
        stop)
            dc down
            ;;
        restart)
            dc down
            dc up -d neo4j schema_bootstrap mcp
            ;;
        logs)
            dc logs -f
            ;;
        status)
            dc ps
            ;;
        clean)
            dc down -v --rmi local
            ;;
        bootstrap)
            dc run --rm schema_bootstrap
            ;;
        index-sources)
            index_sources
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            echo "Unknown command: ${command}"
            show_usage
            exit 1
            ;;
    esac
done
