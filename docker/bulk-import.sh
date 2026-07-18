#!/bin/sh
set -eu

# `neo4j-admin` runs in the Neo4j image while CodeKG writes the CSV files from
# the application image.  The filename is deliberately the only contract here:
# it lets the importer accept any non-empty set of CodeKG node and relationship
# kinds without parsing application-owned JSON in this minimal image.
set -- neo4j-admin database import full neo4j --id-type=string --multiline-fields=true --overwrite-destination=true

for file in /import/nodes_*.csv; do
    [ -e "$file" ] || continue
    label=${file##*/nodes_}
    label=${label%.csv}
    set -- "$@" "--nodes=${label}=${file}"
done

for file in /import/relationships_*.csv; do
    [ -e "$file" ] || continue
    kind=${file##*/relationships_}
    kind=${kind%.csv}
    set -- "$@" "--relationships=${kind}=${file}"
done

exec "$@"
