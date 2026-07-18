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
    echo "  index-sources  Index every repository under CODEKG_REPOS_ROOT"
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

dc() {
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
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

index_sources() {
    local configured entry candidate resolved basename remaining has_more
    local -a repositories=()
    local -A resolved_paths=() basenames=()

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

    for resolved in "${repositories[@]}"; do
        basename="${resolved##*/}"
        dc run --rm -v "$resolved:/repos/$basename:ro" ingestion codekg reindex "/repos/$basename"
    done
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
