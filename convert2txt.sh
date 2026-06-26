#!/bin/bash

# Project to Single TXT File Converter with Git Integration
# Usage: ./convert2txt.sh [project_directory] [output_file]
# 
# This script exports tracked and untracked project files to a single text file,
# while respecting .gitignore and excluding local temp/input data.

# Verbose mode (set to 1 to enable, 0 to disable)
VERBOSE=1

# Set default values
PROJECT_DIR="${1:-.}"
OUTPUT_FILE="${2:-project_export.txt}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd -P)"
OUTPUT_FILE_ABS="$(cd "$(dirname "$OUTPUT_FILE")" && pwd -P)/$(basename "$OUTPUT_FILE")"

# Check if we're in a git repository
if ! git -C "$PROJECT_DIR" rev-parse --git-dir > /dev/null 2>&1; then
    echo "Error: Not a git repository: $PROJECT_DIR"
    exit 1
fi

rm -f "$OUTPUT_FILE_ABS" || true

# File extensions to include (modify as needed)
# Empty means all git-tracked files will be included
EXTENSIONS=(
    "py"
    "txt"
    "md"
    "json"
    "yaml"
    "yml"
    "sql"
    "sh"
    "cfg"
    "ini"
    "toml"
    "example"
    "gitignore"
    "dockerignore"
    "Dockerfile"
)

# Additional patterns to exclude even if tracked by git (optional)
# These will be filtered out from git ls-files results
ADDITIONAL_EXCLUDE_PATTERNS=(
    ".git/*"
    ".pytest_cache/*"
    ".ruff_cache/*"
    ".venv/*"
    "__pycache__/*"
    "sources/*"
    "third_party/CodeGraphContext/.git/*"
    "third_party/CodeGraphContext/.pytest_cache/*"
    "third_party/CodeGraphContext/.ruff_cache/*"
    "third_party/CodeGraphContext/**/__pycache__/*"
    "third_party/CodeGraphContext/website/node_modules/*"
    "third_party/CodeGraphContext/website/dist/*"
    "project_export*.txt"
    "*.pyc"
    "*.pyo"
    "*.egg-info/*"
)

should_exclude_third_party_nested() {
    local file="$1"

    if [[ "$file" != third_party/* ]]; then
        return 1
    fi

    IFS='/' read -r -a parts <<< "$file"

    # Keep only files directly under a directly vendored dependency, such as
    # third_party/CodeGraphContext/README.md. Exclude nested modules/content.
    if (( ${#parts[@]} > 3 )); then
        log_verbose "Excluding nested third-party file: $file"
        return 0
    fi

    return 1
}

echo "Converting project to single TXT file..."
echo "Project Directory: $PROJECT_DIR"
echo "Output File: $OUTPUT_FILE"
echo "Using git to determine files (tracked + untracked, respecting .gitignore)"
echo "----------------------------------------"

# Create or clear the output file
> "$OUTPUT_FILE_ABS"

# Add header
cat << EOF >> "$OUTPUT_FILE_ABS"
================================================================================
PROJECT EXPORT - CodeKG
================================================================================
Generated on: $(date)
Project Directory: $PROJECT_DIR
Export File: $OUTPUT_FILE_ABS
Source: Git-tracked and untracked non-ignored files
================================================================================

TABLE OF CONTENTS:
EOF

# Function to print verbose messages
log_verbose() {
    if [[ "$VERBOSE" == "1" ]]; then
        echo "[VERBOSE] $1"
    fi
}

# Function to check if file matches additional exclude patterns
should_exclude_additional() {
    local file="$1"
    local full_path="$PROJECT_DIR/$file"

    if [[ "$(cd "$(dirname "$full_path")" && pwd -P)/$(basename "$full_path")" == "$OUTPUT_FILE_ABS" ]]; then
        log_verbose "Excluding output file: $file"
        return 0
    fi

    if should_exclude_third_party_nested "$file"; then
        return 0
    fi
    
    if [ ${#ADDITIONAL_EXCLUDE_PATTERNS[@]} -eq 0 ]; then
        return 1
    fi
    
    for pattern in "${ADDITIONAL_EXCLUDE_PATTERNS[@]}"; do
        if [[ "$file" == $pattern ]]; then
            log_verbose "Excluding by additional pattern: $file (pattern: $pattern)"
            return 0
        fi
    done
    return 1
}

# Function to check if file extension is allowed
has_allowed_extension() {
    local file="$1"
    local basename
    basename=$(basename "$file")
    
    # If EXTENSIONS is empty, allow all files
    if [ ${#EXTENSIONS[@]} -eq 0 ]; then
        return 0
    fi
    
    # Check for Dockerfile specifically
    if [[ "$basename" == "Dockerfile" ]] || [[ "$basename" == Dockerfile* ]]; then
        return 0
    fi
    
    # Check if file has no extension (might be a script or config)
    if [[ ! "$basename" =~ \. ]]; then
        return 0
    fi
    
    # Check extension against allowed list
    local extension="${file##*.}"
    if [[ " ${EXTENSIONS[*]} " =~ " ${extension} " ]]; then
        return 0
    fi
    
    return 1
}

# Build file list and table of contents using git
declare -a files_to_process
file_count=0

# Get all tracked files plus untracked files that are not ignored by .gitignore.
while IFS= read -r file; do
    # Skip empty lines
    [[ -z "$file" ]] && continue
    
    # Construct full path
    full_path="$PROJECT_DIR/$file"
    
    # Skip if file doesn't exist (might be deleted but still in index)
    if [[ ! -f "$full_path" ]]; then
        log_verbose "Skipping non-existent file: $file"
        continue
    fi
    
    log_verbose "Found project file: $file"
    
    # Check additional exclude patterns
    if should_exclude_additional "$file"; then
        continue
    fi
    
    # Check if file extension is allowed
    if ! has_allowed_extension "$file"; then
        log_verbose "Skipping file (extension not allowed): $file"
        continue
    fi
    
    log_verbose "Including file: $file"
    files_to_process+=("$full_path")
    echo "- $file" >> "$OUTPUT_FILE_ABS"
    ((file_count++))
done < <(git -C "$PROJECT_DIR" ls-files --cached --others --exclude-standard 2>/dev/null | sort)

echo "" >> "$OUTPUT_FILE_ABS"
echo "Total Files: $file_count" >> "$OUTPUT_FILE_ABS"
echo "" >> "$OUTPUT_FILE_ABS"

# Process each file
for file in "${files_to_process[@]}"; do
    relative_path=${file#$PROJECT_DIR/}
    [[ "$relative_path" == "$file" ]] && relative_path="$file"
    log_verbose "Processing file: $relative_path"
    echo "Processing: $relative_path"
    
    # Add file separator
    cat << EOF >> "$OUTPUT_FILE_ABS"
================================================================================
FILE: $relative_path
================================================================================
EOF
    
    # Check if file is binary
    if file "$file" | grep -q "text"; then
        log_verbose "Adding text content for: $relative_path"
        # Add file content
        cat "$file" >> "$OUTPUT_FILE_ABS"
    else
        log_verbose "Skipping binary file content: $relative_path"
        echo "[BINARY FILE - Content not included]" >> "$OUTPUT_FILE_ABS"
    fi
    
    # Add spacing
    echo -e "\n\n" >> "$OUTPUT_FILE_ABS"
done

# Add footer
cat << 'FOOTER_EOF' >> "$OUTPUT_FILE_ABS"
================================================================================
END OF PROJECT EXPORT - CodeKG
================================================================================
FOOTER_EOF

cat << EOF >> "$OUTPUT_FILE_ABS"
Total files processed: $file_count
Generated on: $(date)
Source: Git-tracked and untracked non-ignored files
EOF

echo "----------------------------------------"
echo "Export completed successfully!"
echo "Files processed: $file_count"
echo "Output file: $OUTPUT_FILE_ABS"
echo "File size: $(du -h "$OUTPUT_FILE_ABS" | cut -f1)"
echo ""
echo "Inclusion rules:"
echo "- Git-tracked and untracked non-ignored files"
echo "- Excludes local temp/input data: .venv, caches, __pycache__, sources"
echo "- Excludes nested third-party content; direct files under third_party/<name>/ may be included"
echo "- Filtered by extension: ${EXTENSIONS[*]}"
echo "- Binary files are marked but content not included"
echo ""
echo "You can now upload '$OUTPUT_FILE_ABS' to your chat interface."
