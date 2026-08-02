#!/bin/bash

# Export the complete evaluation and benchmark harness to one text file.
# Usage: ./evaluation/convert2txt.sh [output_file]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
OUTPUT_FILE="${1:-$SCRIPT_DIR/evaluation_harness_export.txt}"
OUTPUT_DIR="$(dirname "$OUTPUT_FILE")"

if [[ ! -d "$OUTPUT_DIR" ]]; then
    echo "Error: Output directory does not exist: $OUTPUT_DIR" >&2
    exit 1
fi

OUTPUT_FILE_ABS="$(cd "$OUTPUT_DIR" && pwd -P)/$(basename "$OUTPUT_FILE")"

is_harness_source() {
    local relative_path="$1"

    case "$relative_path" in
        __pycache__/*|*/__pycache__/*|.pytest_cache/*|*/.pytest_cache/*)
            return 1
            ;;
        results/*|runs/*|output/*|artifacts/*)
            return 1
            ;;
        report.json|report-*.json|*.pyc|*.pyo|*.log)
            return 1
            ;;
        *.py|*.json|*.txt|*.md|*.sh|*.toml|*.yaml|*.yml)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

declare -a files_to_process=()

while IFS= read -r -d '' repository_path; do
    relative_path="${repository_path#evaluation/}"
    full_path="$PROJECT_DIR/$repository_path"

    [[ -f "$full_path" ]] || continue
    [[ "$(cd "$(dirname "$full_path")" && pwd -P)/$(basename "$full_path")" == "$OUTPUT_FILE_ABS" ]] && continue
    is_harness_source "$relative_path" || continue
    files_to_process+=("$relative_path")
done < <(
    git -C "$PROJECT_DIR" ls-files -z --cached --others --exclude-standard -- evaluation \
        | sort -z
)

: > "$OUTPUT_FILE_ABS"

cat <<EOF >> "$OUTPUT_FILE_ABS"
================================================================================
EVALUATION HARNESS EXPORT - CodeKG
================================================================================
Generated on: $(date)
Harness directory: $SCRIPT_DIR
Export file: $OUTPUT_FILE_ABS
Source: Git-tracked and untracked non-ignored evaluation harness files
================================================================================

TABLE OF CONTENTS:
EOF

for relative_path in "${files_to_process[@]}"; do
    printf -- '- %s\n' "$relative_path" >> "$OUTPUT_FILE_ABS"
done

printf '\nTotal Files: %d\n\n' "${#files_to_process[@]}" >> "$OUTPUT_FILE_ABS"

for relative_path in "${files_to_process[@]}"; do
    full_path="$SCRIPT_DIR/$relative_path"
    cat <<EOF >> "$OUTPUT_FILE_ABS"
================================================================================
FILE: $relative_path
================================================================================
EOF
    cat "$full_path" >> "$OUTPUT_FILE_ABS"
    printf '\n\n\n' >> "$OUTPUT_FILE_ABS"
done

cat <<EOF >> "$OUTPUT_FILE_ABS"
================================================================================
END OF EVALUATION HARNESS EXPORT - CodeKG
================================================================================
Total files processed: ${#files_to_process[@]}
Generated on: $(date)
EOF

echo "Evaluation harness export completed successfully."
echo "Files processed: ${#files_to_process[@]}"
echo "Output file: $OUTPUT_FILE_ABS"
echo "File size: $(du -h "$OUTPUT_FILE_ABS" | cut -f1)"
