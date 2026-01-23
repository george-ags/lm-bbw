#!/bin/bash

# ==============================================================================
# Script Name: cleanup_old_files.sh
# Description: Deletes files older than 40 days in a specified directory.
# Usage: ./cleanup_old_files.sh /path/to/folder
# ==============================================================================

TARGET_DIR="$1"
# Use the second argument for days, defaulting to 60 if not provided
DAYS="${2:-60}"

# Use the third argument for days to archive file, defaulting to 7 if not provided
ADAYS="${3:-7}"

# 1. Input Validation
if [ -z "$TARGET_DIR" ]; then
    echo "Error: No directory specified."
    echo "Usage: $0 <path_to_directory> [days]"
    exit 1
fi

if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: Directory '$TARGET_DIR' does not exist."
    exit 1
fi

# Validate that DAYS is a positive integer
if ! [[ "$DAYS" =~ ^[0-9]+$ ]]; then
    echo "Error: Days must be a positive integer."
    exit 1
fi

# Validate that ADAYS is a positive integer
if ! [[ "$ADAYS" =~ ^[0-9]+$ ]]; then
    echo "Error: Days must be a positive integer."
    exit 1
fi

# 2. Execution
echo "Scanning '$TARGET_DIR' for files older than $DAYS days..."

# The find command works as follows:
# -type f    : Look for files only (ignore directories)
# -mtime +40 : Modified more than 40 days ago
# -print     : Print the name of the file (so you see what is deleted)
# -delete    : Delete the file

find "$TARGET_DIR" -type f -mtime +$DAYS -print -delete

find "$TARGET_DIR" -maxdepth 1 -type f -mtime +$ADAYS -exec mv {} "$TARGET_DIR"/archive \;

echo "Cleanup complete."

