#!/usr/bin/env bash
# =============================================================================
# sync-api-specs.sh — Pull latest OpenAPI specs and detect changes
#
# This script downloads the latest Amazon Ads Unified API OpenAPI specs,
# compares them with the previously downloaded versions, and reports changes
# that may require updating the migration skills.
#
# Usage:
#   ./scripts/sync-api-specs.sh           # Pull specs and show diff summary
#   ./scripts/sync-api-specs.sh --check   # CI mode: exit 1 if specs changed
#   ./scripts/sync-api-specs.sh --force   # Force re-download even if unchanged
# =============================================================================

set -euo pipefail

# --- Configuration -----------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPECS_DIR="$PROJECT_ROOT/api-specs"

# Spec definitions (parallel arrays for bash 3.x compatibility)
SPEC_NAMES=("unified-api-sp" "unified-api-sb" "unified-api-spglobal" "unified-api-dsp")
SPEC_URLS=(
  "https://d1y2lf8k3vrkfu.cloudfront.net/openapi/en-us/dest/AmazonAdsAPISPMerged_prod_3p.json"
  "https://d1y2lf8k3vrkfu.cloudfront.net/openapi/en-us/dest/AmazonAdsAPISBMerged_prod_3p.json"
  "https://d1y2lf8k3vrkfu.cloudfront.net/openapi/en-us/dest/AmazonAdsAPISPGLOBALMerged_prod_3p.json"
  "https://d1y2lf8k3vrkfu.cloudfront.net/openapi/en-us/dest/AmazonAdsAPIDSPMerged_prod_3p.json"
)
SPEC_SKILLS=("unified-sp-migration" "unified-sb-migration" "amazon-ads-spglobal" "unified-dsp-cm-migration")

# --- Colors ------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- Functions ---------------------------------------------------------------

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

check_dependencies() {
  local missing=()
  for cmd in curl jq diff; do
    if ! command -v "$cmd" &>/dev/null; then
      missing+=("$cmd")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    log_error "Missing required tools: ${missing[*]}"
    echo "  Install with: brew install ${missing[*]}"
    exit 1
  fi
}

download_spec() {
  local idx="$1"
  local name="${SPEC_NAMES[$idx]}"
  local url="${SPEC_URLS[$idx]}"
  local output="$SPECS_DIR/.${name}.new.json"
  local temp_file="$SPECS_DIR/.${name}.tmp.json"

  log_info "Downloading $name spec..."

  if ! curl -sSfL "$url" -o "$temp_file" 2>/dev/null; then
    log_error "Failed to download $name from $url"
    rm -f "$temp_file"
    return 1
  fi

  # Validate JSON
  if ! jq empty "$temp_file" 2>/dev/null; then
    log_error "Downloaded file is not valid JSON: $name"
    rm -f "$temp_file"
    return 1
  fi

  # Pretty-print for consistent diffing
  jq --sort-keys '.' "$temp_file" > "${temp_file}.formatted"
  mv "${temp_file}.formatted" "$temp_file"
  rm -f "${temp_file}.formatted" 2>/dev/null || true

  mv "$temp_file" "$output"
  log_ok "Downloaded $name ($(wc -c < "$output" | tr -d ' ') bytes)"
  return 0
}

compare_spec() {
  local name="$1"
  local current="$SPECS_DIR/${name}.json"
  local new_file="$SPECS_DIR/.${name}.new.json"

  if [[ ! -f "$current" ]]; then
    log_warn "$name: No previous version found (first run)"
    return 2  # new spec, no comparison possible
  fi

  if diff -q "$current" "$new_file" &>/dev/null; then
    log_ok "$name: No changes detected"
    return 0
  else
    return 1  # changes detected
  fi
}

show_changes() {
  set +e  # diff returns 1 when files differ — don't exit
  local idx="$1"
  local name="${SPEC_NAMES[$idx]}"
  local old_file="$SPECS_DIR/${name}.json"
  local new_file="$SPECS_DIR/.${name}.new.json"
  local skill="${SPEC_SKILLS[$idx]}"

  echo ""
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${YELLOW}  CHANGES DETECTED: $name${NC}"
  echo -e "${YELLOW}  Related skill: skills/$skill/SKILL.md${NC}"
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""

  # Use temp files for bash 3.x compatibility (no process substitution issues)
  local tmp_old="$SPECS_DIR/.tmp_old_keys"
  local tmp_new="$SPECS_DIR/.tmp_new_keys"

  # Endpoint changes
  jq -r '.paths | keys[]' "$old_file" 2>/dev/null | sort > "$tmp_old"
  jq -r '.paths | keys[]' "$new_file" 2>/dev/null | sort > "$tmp_new"

  local paths_added paths_removed
  paths_added=$(diff "$tmp_old" "$tmp_new" | grep '^>' | wc -l | tr -d ' ')
  paths_removed=$(diff "$tmp_old" "$tmp_new" | grep '^<' | wc -l | tr -d ' ')

  echo "  Endpoint changes:"
  echo "    + $paths_added new paths"
  echo "    - $paths_removed removed paths"
  echo ""

  if [[ "$paths_added" -gt 0 ]]; then
    echo "  New endpoints:"
    diff "$tmp_old" "$tmp_new" | grep '^>' | sed 's/^> /    + /' | head -20
    echo ""
  fi

  if [[ "$paths_removed" -gt 0 ]]; then
    echo "  Removed endpoints:"
    diff "$tmp_old" "$tmp_new" | grep '^<' | sed 's/^< /    - /' | head -20
    echo ""
  fi

  # Schema changes
  jq -r '.components.schemas | keys[]' "$old_file" 2>/dev/null | sort > "$tmp_old"
  jq -r '.components.schemas | keys[]' "$new_file" 2>/dev/null | sort > "$tmp_new"

  local schemas_before schemas_after
  schemas_before=$(wc -l < "$tmp_old" | tr -d ' ')
  schemas_after=$(wc -l < "$tmp_new" | tr -d ' ')
  echo "  Schema count: $schemas_before → $schemas_after"
  echo ""

  local new_schemas
  new_schemas=$(diff "$tmp_old" "$tmp_new" | grep '^>' | sed 's/^> //' | head -10)
  if [[ -n "$new_schemas" ]]; then
    echo "  New schemas:"
    echo "$new_schemas" | sed 's/^/    + /'
    echo ""
  fi

  rm -f "$tmp_old" "$tmp_new"

  # Action items
  echo -e "  ${RED}⚠️  Action required:${NC}"
  echo "    1. Ask Kiro: \"Update migration skills from the latest diff\""
  echo "    2. Or manually review: api-specs/diff/${name}.changes.md"
  echo ""

  # Save old/new to diff/ directory for skill-based updating
  save_diff "$name"
  set -e
}

save_as_previous() {
  local name="$1"
  local current="$SPECS_DIR/${name}.json"
  local previous="$SPECS_DIR/.${name}.previous.json"
  cp "$current" "$previous"
}

save_diff() {
  local name="$1"
  local old="$SPECS_DIR/${name}.json"
  local new="$SPECS_DIR/.${name}.new.json"
  local diff_dir="$SPECS_DIR/diff"

  mkdir -p "$diff_dir"

  # Save old and new versions with clear naming
  cp "$old" "$diff_dir/${name}.old.json"
  cp "$new" "$diff_dir/${name}.new.json"

  # Generate a summary diff file (endpoints + schemas changes)
  {
    echo "# Diff Summary: $name"
    echo "# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    echo ""
    echo "## New Endpoints"
    diff <(jq -r '.paths | keys[]' "$old" 2>/dev/null | sort) \
         <(jq -r '.paths | keys[]' "$new" 2>/dev/null | sort) \
    | grep '^>' | sed 's/^> //' || echo "(none)"
    echo ""
    echo "## Removed Endpoints"
    diff <(jq -r '.paths | keys[]' "$old" 2>/dev/null | sort) \
         <(jq -r '.paths | keys[]' "$new" 2>/dev/null | sort) \
    | grep '^<' | sed 's/^< //' || echo "(none)"
    echo ""
    echo "## New Schemas"
    diff <(jq -r '.components.schemas | keys[]' "$old" 2>/dev/null | sort) \
         <(jq -r '.components.schemas | keys[]' "$new" 2>/dev/null | sort) \
    | grep '^>' | sed 's/^> //' || echo "(none)"
    echo ""
    echo "## Removed Schemas"
    diff <(jq -r '.components.schemas | keys[]' "$old" 2>/dev/null | sort) \
         <(jq -r '.components.schemas | keys[]' "$new" 2>/dev/null | sort) \
    | grep '^<' | sed 's/^< //' || echo "(none)"
  } > "$diff_dir/${name}.changes.md"

  log_ok "Saved diff: api-specs/diff/${name}.{old,new,changes}"
}

generate_report() {
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  local report_file="$SPECS_DIR/.last-sync-report.json"

  local report="{\"timestamp\": \"$timestamp\", \"specs\": {"
  local first=true
  for idx in "${!SPEC_NAMES[@]}"; do
    local name="${SPEC_NAMES[$idx]}"
    local url="${SPEC_URLS[$idx]}"
    local skill="${SPEC_SKILLS[$idx]}"
    if [[ "$first" != "true" ]]; then report+=","; fi
    first=false
    local size="0"
    if [[ -f "$SPECS_DIR/${name}.json" ]]; then
      size=$(wc -c < "$SPECS_DIR/${name}.json" | tr -d ' ')
    fi
    report+="\"$name\": {\"url\": \"$url\", \"size\": $size, \"skill\": \"$skill\"}"
  done
  report+="}}"

  echo "$report" | jq '.' > "$report_file"
}

# --- Main --------------------------------------------------------------------

main() {
  local mode="normal"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check) mode="check" ;;
      --force) mode="force" ;;
      --help|-h)
        echo "Usage: $0 [--check|--force|--help]"
        echo ""
        echo "Options:"
        echo "  --check   CI mode: exit 1 if any spec has changed"
        echo "  --force   Force re-download and overwrite previous versions"
        echo "  --help    Show this help message"
        exit 0
        ;;
      *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
  done

  echo ""
  echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BLUE}║  Amazon Ads API Spec Sync                                    ║${NC}"
  echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo ""

  check_dependencies
  mkdir -p "$SPECS_DIR"

  # Step 1: Download all specs to .new.json temp files
  local download_failed=false
  for idx in "${!SPEC_NAMES[@]}"; do
    if ! download_spec "$idx"; then
      download_failed=true
    fi
  done

  if [[ "$download_failed" == "true" ]]; then
    log_error "Some downloads failed. Check network connectivity."
    exit 1
  fi

  echo ""

  # Step 2: Compare CURRENT (from last run) vs NEW (just downloaded)
  local has_changes=false
  for idx in "${!SPEC_NAMES[@]}"; do
    local name="${SPEC_NAMES[$idx]}"
    local result
    set +e
    compare_spec "$name"
    result=$?
    set -e

    case $result in
      0) ;; # no changes
      1) has_changes=true; show_changes "$idx" ;;
      2) has_changes=true ;; # new spec (first run)
    esac
  done

  # Step 3: Rotate — current → previous, new → current
  for idx in "${!SPEC_NAMES[@]}"; do
    local name="${SPEC_NAMES[$idx]}"
    local current="$SPECS_DIR/${name}.json"
    local new_file="$SPECS_DIR/.${name}.new.json"
    if [[ -f "$current" ]]; then
      save_as_previous "$name"
    fi
    if [[ -f "$new_file" ]]; then
      mv "$new_file" "$current"
    fi
  done

  # Generate sync report
  generate_report

  echo ""
  if [[ "$has_changes" == "true" ]]; then
    echo -e "${YELLOW}┌─────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${YELLOW}│  ⚠️  API specs have changed! Diffs saved to api-specs/diff/   │${NC}"
    echo -e "${YELLOW}│                                                             │${NC}"
    echo -e "${YELLOW}│  Next steps:                                                │${NC}"
    echo -e "${YELLOW}│  1. Ask Kiro: \"Update migration skills from the latest diff\"│${NC}"
    echo -e "${YELLOW}│     (uses the update-migration-skills skill)                │${NC}"
    echo -e "${YELLOW}│  2. Review Kiro's changes and commit                        │${NC}"
    echo -e "${YELLOW}└─────────────────────────────────────────────────────────────┘${NC}"

    if [[ "$mode" == "check" ]]; then
      exit 1
    fi
  else
    echo -e "${GREEN}┌─────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${GREEN}│  ✅ All API specs are up to date. No skill changes needed.   │${NC}"
    echo -e "${GREEN}└─────────────────────────────────────────────────────────────┘${NC}"
  fi
  echo ""
}

main "$@"
