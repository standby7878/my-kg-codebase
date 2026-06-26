#!/bin/bash
set -e

# ========================================
# Unified Neo4j Data Import Init Container
# Combines S3 Download and Neo4j Data Ingestion
# ========================================

# Configuration
S3_BUCKET="bcunifyapp-dev-euc1-s3-unify-datapipeline-storage"
S3_PREFIX="neo4j-knowledge-graph"
STAGING_DIR="/data/staging"
IMPORT_DIR="/data/import"
BACKUP_DIR="/data/import/backup"
DATA_DIR="/data"
DB_NAME="neo4j"
NODES_FILE="${IMPORT_DIR}/neo4j_nodes.csv"
RELATIONSHIPS_FILE="${IMPORT_DIR}/neo4j_relationships.csv"
STAGING_NODES_GZ="${STAGING_DIR}/neo4j_nodes.csv.gz"
STAGING_RELATIONSHIPS_GZ="${STAGING_DIR}/neo4j_relationships.csv.gz"
ARRAY_DELIMITER="|"
VERBOSE_FLAG="--verbose"
READY_MARKER_RETRIES=3
READY_MARKER_WAIT=60

echo "=========================================="
echo "Unified Neo4j Data Import Init Container"
echo "=========================================="
echo "Timestamp: $(date)"
echo "S3 Bucket: s3://${S3_BUCKET}/${S3_PREFIX}"
echo "Database: $DB_NAME"
echo "Staging directory: $STAGING_DIR"
echo "Import directory: $IMPORT_DIR"
echo "Data directory: $DATA_DIR"
echo ""

# ========================================
# PART 1: S3 DATA DOWNLOAD
# ========================================

# ========================================
# PHASE 1: Version Discovery
# ========================================
echo "=========================================="
echo "PHASE 1: S3 Version Discovery"
echo "=========================================="

echo "Cleaning staging directory from old archives"
rm -f "${STAGING_DIR}"/*.gz || true

echo "Listing S3 folders..."
LATEST_VERSION=""
LATEST_TIMESTAMP=0

# List all prefixes (folders) in S3
S3_FOLDERS=$(aws s3 ls s3://${S3_BUCKET}/${S3_PREFIX}/ | grep 'PRE' | awk '{print $2}' | sed 's#/##g' || true)

if [ -z "$S3_FOLDERS" ]; then
  echo "WARNING: No folders found in s3://${S3_BUCKET}/${S3_PREFIX}/"
  echo "Skipping download - Neo4j will start with existing data"
  exit 0
fi

echo "Found S3 folders:"
echo "$S3_FOLDERS" | while read folder; do echo "  - $folder"; done
echo ""

# Parse and find latest version
for folder in $S3_FOLDERS; do
  # Try parsing DD-MM-YYYY-HH-MM format (extended with hours and minutes)
  if [[ $folder =~ ^([0-9]{2})-([0-9]{2})-([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]]; then
    day="${BASH_REMATCH[1]}"
    month="${BASH_REMATCH[2]}"
    year="${BASH_REMATCH[3]}"
    hour="${BASH_REMATCH[4]}"
    minute="${BASH_REMATCH[5]}"
    
    # Convert to timestamp (YYYY-MM-DD HH:MM format for date command)
    date_str="${year}-${month}-${day} ${hour}:${minute}"
    if timestamp=$(date -d "$date_str" +%s 2>/dev/null); then
      echo "  ✓ Valid date: $folder (timestamp: $timestamp, $(date -d @$timestamp '+%Y-%m-%d %H:%M'))"
      
      if [ "$timestamp" -gt "$LATEST_TIMESTAMP" ]; then
        LATEST_TIMESTAMP=$timestamp
        LATEST_VERSION=$folder
      fi
    else
      echo "  ✗ Invalid date: $folder"
    fi
  # Fallback to legacy DD-MM-YYYY format (treat as 00:00)
  elif [[ $folder =~ ^([0-9]{2})-([0-9]{2})-([0-9]{4})$ ]]; then
    day="${BASH_REMATCH[1]}"
    month="${BASH_REMATCH[2]}"
    year="${BASH_REMATCH[3]}"
    # Convert to timestamp at midnight (00:00)
    date_str="${year}-${month}-${day} 00:00"
    if timestamp=$(date -d "$date_str" +%s 2>/dev/null); then
      echo "  ✓ Valid date (legacy): $folder (timestamp: $timestamp, $(date -d @$timestamp '+%Y-%m-%d %H:%M') - treated as 00:00)"
      if [ "$timestamp" -gt "$LATEST_TIMESTAMP" ]; then
        LATEST_TIMESTAMP=$timestamp
        LATEST_VERSION=$folder
      fi
    else
      echo "  ✗ Invalid date: $folder"
    fi
  else
    echo "  ✗ Does not match DD-MM-YYYY-HH-MM or DD-MM-YYYY: $folder"
  fi
done

if [ -z "$LATEST_VERSION" ]; then
  echo ""
  echo "WARNING: No valid version folders found in S3"
  echo "Skipping download - Neo4j will start with existing data"
  exit 0
fi

echo ""
echo "✓ Latest S3 version: $LATEST_VERSION"
echo "  Timestamp: $LATEST_TIMESTAMP ($(date -d @$LATEST_TIMESTAMP))"
echo ""

# ========================================
# PHASE 2: Forced Upload Check
# ========================================
echo "=========================================="
echo "PHASE 2: Forced Upload Check"
echo "=========================================="

S3_VERSION_PATH="s3://${S3_BUCKET}/${S3_PREFIX}/${LATEST_VERSION}"
FORCED_UPLOAD_MARKER="${S3_VERSION_PATH}/forced_upload/"

echo "Checking for forced upload marker at: $FORCED_UPLOAD_MARKER"
FORCE_DOWNLOAD=false

if aws s3 ls "$FORCED_UPLOAD_MARKER" >/dev/null 2>&1; then
  echo "  ✓ Forced upload marker found!"
  echo "  → Download will be forced regardless of version"
  FORCE_DOWNLOAD=true
else
  echo "  ✗ No forced upload marker"
  echo "  → Normal version comparison will be used"
fi
echo ""

# ========================================
# PHASE 3: Version Comparison
# ========================================
echo "=========================================="
echo "PHASE 3: Version Comparison"
echo "=========================================="

NEEDS_DOWNLOAD=false
if [ "$FORCE_DOWNLOAD" = true ]; then
  echo "Forced upload mode enabled - bypassing version check"
  NEEDS_DOWNLOAD=true
# Check if staging has a version marker
elif [ -f "${STAGING_DIR}/.version" ]; then
  STAGING_VERSION=$(cat "${STAGING_DIR}/.version")
  echo "Current staging version: $STAGING_VERSION"
  
  if [ "$STAGING_VERSION" = "$LATEST_VERSION" ]; then
    echo "  → Staging data is UP-TO-DATE with S3"
    echo "  → Skipping download"
    NEEDS_DOWNLOAD=false
  else
    echo "  → Staging version differs from S3 latest"
    echo "  → Update required"
    NEEDS_DOWNLOAD=true
  fi
elif [ -f "$RELATIONSHIPS_FILE" ]; then
  # Fallback: compare with existing import data
  EXISTING_TIMESTAMP=$(stat -c %Y "$RELATIONSHIPS_FILE" 2>/dev/null || echo 0)
  echo "Existing data timestamp: $EXISTING_TIMESTAMP ($(date -d @$EXISTING_TIMESTAMP 2>/dev/null || echo 'N/A'))"
  echo "S3 version timestamp:   $LATEST_TIMESTAMP ($(date -d @$LATEST_TIMESTAMP))"
  
  if [ "$LATEST_TIMESTAMP" -gt "$EXISTING_TIMESTAMP" ]; then
    echo "  → S3 version is NEWER"
    echo "  → Download required"
    NEEDS_DOWNLOAD=true
  else
    echo "  → Current data is UP-TO-DATE"
    echo "  → Skipping download"
    NEEDS_DOWNLOAD=false
  fi
else
  echo "No existing data found"
  echo "  → Fresh installation - download required"
  NEEDS_DOWNLOAD=true
fi

if [ "$NEEDS_DOWNLOAD" = false ]; then
  echo ""
  echo "=========================================="
  echo "Data is current - skipping download"
  echo "=========================================="
  exit 0
fi
echo ""

# ========================================
# PHASE 4: Ready Marker Check
# ========================================
echo "=========================================="
echo "PHASE 4: Ready Marker Check"
echo "=========================================="

READY_MARKER_PATH="${S3_VERSION_PATH}/data_is_ready/"
echo "Checking for ready marker at: $READY_MARKER_PATH"

MARKER_FOUND=false
for attempt in $(seq 1 $READY_MARKER_RETRIES); do
  echo "  Attempt $attempt of $READY_MARKER_RETRIES..."
  
  # Check if the folder exists in S3
  if aws s3 ls "$READY_MARKER_PATH" >/dev/null 2>&1; then
    echo "  ✓ Ready marker found!"
    MARKER_FOUND=true
    break
  fi
  
  if [ "$attempt" -lt "$READY_MARKER_RETRIES" ]; then
    echo "  ✗ Ready marker not found - waiting ${READY_MARKER_WAIT} seconds..."
    sleep $READY_MARKER_WAIT
  fi
done

if [ "$MARKER_FOUND" = false ]; then
  echo ""
  echo "WARNING: Ready marker not found after $READY_MARKER_RETRIES attempts"
  echo "Skipping download - Neo4j will start with existing/old data"
  echo "=========================================="
  exit 0
fi
echo ""

# ========================================
# PHASE 5: Download from S3
# ========================================
echo "=========================================="
echo "PHASE 5: Download from S3"
echo "=========================================="

# Clean staging directory (preserve .version file)
echo "Cleaning old staging files..."
mkdir -p "$STAGING_DIR"
# Delete only .gz files, preserve .version
rm -f "${STAGING_DIR}"/*.gz 2>/dev/null || true
echo "  ✓ Old .gz files removed (preserved .version file)"
echo ""

# Download compressed files
echo "Downloading neo4j_nodes.csv.gz..."
if aws s3 cp "${S3_VERSION_PATH}/neo4j_nodes.csv.gz" "${STAGING_DIR}/neo4j_nodes.csv.gz"; then
  SIZE=$(du -h "${STAGING_DIR}/neo4j_nodes.csv.gz" | cut -f1)
  echo "  ✓ Downloaded successfully ($SIZE)"
else
  echo "  ✗ Download failed"
  echo "WARNING: Failed to download nodes file - skipping"
  exit 0
fi

echo "Downloading neo4j_relationships.csv.gz..."
if aws s3 cp "${S3_VERSION_PATH}/neo4j_relationships.csv.gz" "${STAGING_DIR}/neo4j_relationships.csv.gz"; then
  SIZE=$(du -h "${STAGING_DIR}/neo4j_relationships.csv.gz" | cut -f1)
  echo "  ✓ Downloaded successfully ($SIZE)"
else
  echo "  ✗ Download failed"
  echo "WARNING: Failed to download relationships file - skipping"
  # Only remove downloaded .gz files, preserve .version
  rm -f "${STAGING_DIR}"/*.gz 2>/dev/null || true
  exit 0
fi

# Create version marker
echo "$LATEST_VERSION" > "${STAGING_DIR}/.version"
echo ""
echo "✓ Version marker created: $LATEST_VERSION"

# Note: File ownership is automatically managed by fsGroup (7474)
echo ""
echo "=========================================="
echo "Download completed successfully"
echo "=========================================="
echo "Downloaded files:"
ls -lh "$STAGING_DIR"
echo ""

# ========================================
# PART 2: NEO4J DATA INGESTION
# ========================================

# ========================================
# PHASE 6: Check Staging Directory
# ========================================
echo "=========================================="
echo "PHASE 6: Check Staging Data"
echo "=========================================="

if [ ! -d "$STAGING_DIR" ] || [ -z "$(ls -A "$STAGING_DIR" 2>/dev/null)" ]; then
  echo "INFO: No data found in staging directory"
  echo "Skipping ingestion - Neo4j will start with existing data"
  exit 0
fi

if [ ! -f "$STAGING_NODES_GZ" ] || [ ! -f "$STAGING_RELATIONSHIPS_GZ" ]; then
  echo "INFO: Staging directory incomplete - missing required .gz files"
  echo "Skipping ingestion - Neo4j will start with existing data"
  exit 0
fi

echo "✓ Staging directory contains data:"
ls -lh "$STAGING_DIR"
echo ""

# ========================================
# PHASE 7: Backup Management
# ========================================
echo "=========================================="
echo "PHASE 7: Backup Management"
echo "=========================================="

if [ -f "$RELATIONSHIPS_FILE" ] || [ -f "$NODES_FILE" ]; then
  echo "Existing CSV files detected - creating backup..."
  
  # Remove old backup
  if [ -d "$BACKUP_DIR" ]; then
    echo "  Removing old backup at $BACKUP_DIR"
    rm -rf "$BACKUP_DIR" 2>/dev/null || true
  fi
  
  # Create fresh backup (gzipped)
  mkdir -p "$BACKUP_DIR"
  echo "  Created backup directory: $BACKUP_DIR"
  
  if [ -f "$RELATIONSHIPS_FILE" ]; then
    gzip -c "$RELATIONSHIPS_FILE" > "$BACKUP_DIR/neo4j_relationships.csv.gz"
    echo "  ✓ Backed up: neo4j_relationships.csv.gz ($(du -h "$BACKUP_DIR/neo4j_relationships.csv.gz" | cut -f1))"
  fi
  
  if [ -f "$NODES_FILE" ]; then
    gzip -c "$NODES_FILE" > "$BACKUP_DIR/neo4j_nodes.csv.gz"
    echo "  ✓ Backed up: neo4j_nodes.csv.gz ($(du -h "$BACKUP_DIR/neo4j_nodes.csv.gz" | cut -f1))"
  fi
  
  echo "  Backup completed successfully"
else
  echo "No existing CSV files to backup"
fi
echo ""

# ========================================
# PHASE 8: Decompression
# ========================================
echo "=========================================="
echo "PHASE 8: Decompression"
echo "=========================================="

echo "Decompressing files from staging to import directory..."
mkdir -p "$IMPORT_DIR"

# Decompress nodes
echo "Decompressing neo4j_nodes.csv.gz..."
gunzip -c "$STAGING_NODES_GZ" > "$NODES_FILE"
echo "  ✓ Decompressed ($(du -h "$NODES_FILE" | cut -f1))"

# Decompress relationships
echo "Decompressing neo4j_relationships.csv.gz..."
gunzip -c "$STAGING_RELATIONSHIPS_GZ" > "$RELATIONSHIPS_FILE"
echo "  ✓ Decompressed ($(du -h "$RELATIONSHIPS_FILE" | cut -f1))"
echo ""

# Display file previews
echo "File previews:"
echo "Nodes file (first 3 lines):"
head -3 "$NODES_FILE"
echo ""
echo "Relationships file (first 3 lines):"
head -3 "$RELATIONSHIPS_FILE"
echo ""

# ========================================
# PHASE 9: Data Ingestion
# ========================================
echo "=========================================="
echo "PHASE 9: Data Ingestion"
echo "=========================================="

# Remove existing database
if [ -d "$DATA_DIR/databases/$DB_NAME" ]; then
  echo "Removing existing database at $DATA_DIR/databases/$DB_NAME"
  rm -rf "$DATA_DIR/databases/$DB_NAME"
  echo "  ✓ Old database removed"
  echo ""
fi

# Function to perform ingestion
perform_ingestion() {
  echo "Starting neo4j-admin database import full"
  echo "Command:"
  echo "  neo4j-admin database import full $DB_NAME \\"
  echo "    --overwrite-destination=true \\"
  echo "    --array-delimiter=\"$ARRAY_DELIMITER\" \\"
  echo "    --nodes=$NODES_FILE \\"
  echo "    --relationships=$RELATIONSHIPS_FILE \\"
  echo "    $VERBOSE_FLAG"
  echo ""

  neo4j-admin database import full "$DB_NAME" \
    --overwrite-destination=true \
    --array-delimiter="$ARRAY_DELIMITER" \
    --nodes="$NODES_FILE" \
    --relationships="$RELATIONSHIPS_FILE" \
    $VERBOSE_FLAG \
    2>&1
  
  return $?
}

# Attempt primary ingestion
if perform_ingestion; then
  echo ""
  echo "=========================================="
  echo "Import completed successfully"
  echo "=========================================="
  echo "Timestamp: $(date)"
  echo ""
  echo "Database statistics:"
  if [ -d "$DATA_DIR/databases/$DB_NAME" ]; then
    echo "Database directory:"
    ls -lh "$DATA_DIR/databases/$DB_NAME/" | head -20
    echo ""
    echo "Database size:"
    du -sh "$DATA_DIR/databases/$DB_NAME/"
  fi
  
  # Remove backup on success
  if [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR" 2>/dev/null || true
    echo ""
    echo "✓ Backup removed (no longer needed)"
  fi
  
  # Clean staging directory after successful ingestion
  echo ""
  echo "Cleaning staging directory..."
  # Note: Files created by aws-cli (root) with fsGroup are owned by group 7474
  # User 7474 can delete them due to directory write permission
  rm -f "${STAGING_DIR}"/*.gz "${STAGING_DIR}/.version" 2>/dev/null || true
  echo "✓ Staging directory cleaned"
  
  echo ""
  echo "Init container completed successfully"
  exit 0
else
  INGESTION_EXIT_CODE=$?
  echo ""
  echo "ERROR: neo4j-admin database import full failed with exit code $INGESTION_EXIT_CODE"
  echo ""
  
  # Check if backup exists for restore
  if [ ! -d "$BACKUP_DIR" ] || [ -z "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]; then
    echo "ERROR: No backup available for restore"
    echo "Init container failed"
    exit 1
  fi
  
  echo "=========================================="
  echo "Attempting Restore from Backup"
  echo "=========================================="
  
  # Restore from backup (gunzip compressed backups)
  echo "Restoring CSV files from backup..."
  if [ -f "$BACKUP_DIR/neo4j_relationships.csv.gz" ]; then
    gunzip -c "$BACKUP_DIR/neo4j_relationships.csv.gz" > "$RELATIONSHIPS_FILE"
    echo "  ✓ Restored: neo4j_relationships.csv (uncompressed from backup)"
  fi
  if [ -f "$BACKUP_DIR/neo4j_nodes.csv.gz" ]; then
    gunzip -c "$BACKUP_DIR/neo4j_nodes.csv.gz" > "$NODES_FILE"
    echo "  ✓ Restored: neo4j_nodes.csv (uncompressed from backup)"
  fi
  echo ""
  
  # Remove failed database
  if [ -d "$DATA_DIR/databases/$DB_NAME" ]; then
    echo "Removing failed database..."
    rm -rf "$DATA_DIR/databases/$DB_NAME"
    echo "  ✓ Failed database removed"
    echo ""
  fi
  
  # Retry ingestion with restored data
  echo "Retrying ingestion with restored backup data..."
  echo ""
  
  if perform_ingestion; then
    echo ""
    echo "=========================================="
    echo "Restore and Re-import Successful"
    echo "=========================================="
    echo "Timestamp: $(date)"
    
    # Remove backup after successful restore
    rm -rf "$BACKUP_DIR" 2>/dev/null || true
    echo "✓ Backup removed"
    echo ""
    echo "Init container completed successfully (with restored data)"
    exit 0
  else
    RESTORE_EXIT_CODE=$?
    echo ""
    echo "ERROR: Restore ingestion also failed with exit code $RESTORE_EXIT_CODE"
    echo "Init container failed - unable to restore database"
    exit 1
  fi
fi
