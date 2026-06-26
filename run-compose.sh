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
    echo "  index-sources  Reindex pghoard, pgbackrest, pglookout, and patroni"
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
            dc run --rm ingestion codekg reindex /repos/pghoard
            dc run --rm ingestion codekg reindex /repos/pgbackrest
            dc run --rm ingestion codekg reindex /repos/pglookout
            dc run --rm ingestion codekg reindex /repos/patroni
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
