#!/bin/sh
set -eu

# `neo4j-admin` runs in the Neo4j image while CodeKG writes the CSV files from
# the application image.  The published manifest is the contract: a v2 group
# contains its header followed by its headerless shards in importer order.
set -- neo4j-admin database import full neo4j --id-type=string --multiline-fields=true --overwrite-destination=true

test -f /import/manifest.json

append_groups() {
    group_kind=$1
    option=$2
    entries=/tmp/codekg-import-entries-$$
    # This is deliberately not piped into the loop: under POSIX sh, a failed
    # jq on the left side of a pipeline can otherwise be masked by `while`.
    jq -r --arg kind "$group_kind" '
        .[$kind] | to_entries[] |
        [.key, (.value.files // [.value.file])] |
        @base64
    ' /import/manifest.json > "$entries"
    while IFS= read -r encoded; do
        decoded=$(printf %s "$encoded" | base64 -d)
        name=$(printf %s "$decoded" | jq -e -r '.[0]')
        files=""
        file_entries=/tmp/codekg-import-files-$$
        printf %s "$decoded" | jq -e -r '.[1][]' > "$file_entries"
        while IFS= read -r relative; do
            case $relative in
                *,*)
                    rm -f "$file_entries" "$entries"
                    echo "unsupported comma in import filename: $relative" >&2
                    exit 1
                    ;;
            esac
            if [ -z "$files" ]; then
                files=/import/$relative
            else
                files=$files,/import/$relative
            fi
        done < "$file_entries"
        rm -f "$file_entries"
        printf '%s\n' "$option=$name=$files" >> /tmp/codekg-import-arguments
    done < "$entries"
    rm -f "$entries"
}

: > /tmp/codekg-import-arguments
append_groups nodes --nodes
append_groups relationships --relationships
while IFS= read -r argument; do
    set -- "$@" "$argument"
done < /tmp/codekg-import-arguments
rm -f /tmp/codekg-import-arguments

exec "$@"
