#!/usr/bin/env bash
# =============================================================================
# extract-enums.sh — Scrape enum definitions from Amazon Ads documentation
#
# Source: https://advertising.amazon.com/API/docs/en-us/reference/common-models/enums
# Extracts content from: <article id="" class="content-container markdown">
#
# This script:
#   1. Downloads the enums documentation page
#   2. Extracts the <article> content (tables of enum values)
#   3. Parses enum tables into structured JSON
#   4. Compares with previous version to detect changes
#   5. Generates a change report
#
# Usage:
#   ./scripts/extract-enums.sh              # Scrape and show changes
#   ./scripts/extract-enums.sh --check      # CI mode: exit 1 if enums changed
#   ./scripts/extract-enums.sh --raw        # Output raw extracted HTML (debug)
#   ./scripts/extract-enums.sh --markdown   # Output parsed markdown
# =============================================================================

set -euo pipefail

# --- Configuration -----------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENUMS_DIR="$PROJECT_ROOT/api-specs"

ENUMS_URL="https://advertising.amazon.com/API/docs/en-us/reference/common-models/enums"
ENUMS_JSON="$ENUMS_DIR/enums-unified-api.json"
ENUMS_MD="$ENUMS_DIR/enums-unified-api.md"
ENUMS_PREVIOUS="$ENUMS_DIR/.enums-unified-api.previous.json"
ENUMS_RAW="$ENUMS_DIR/.enums-raw.html"

# User-Agent to avoid being blocked
USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# --- Colors ------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- Functions ---------------------------------------------------------------

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

check_dependencies() {
  local missing=()
  for cmd in curl jq python3; do
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

download_page() {
  log_info "Downloading enums page from Amazon Ads docs..."

  local temp_html="$ENUMS_DIR/.enums-page.html"

  if ! curl -sSfL \
    -H "User-Agent: $USER_AGENT" \
    -H "Accept: text/html,application/xhtml+xml" \
    -H "Accept-Language: en-US,en;q=0.9" \
    "$ENUMS_URL" -o "$temp_html" 2>/dev/null; then
    log_error "Failed to download page from $ENUMS_URL"
    rm -f "$temp_html"
    return 1
  fi

  local size
  size=$(wc -c < "$temp_html" | tr -d ' ')
  log_ok "Downloaded page ($size bytes)"

  echo "$temp_html"
}

extract_article() {
  local html_file="$1"

  # Extract content from <article class="content-container markdown"> ... </article>
  # Using Python for robust HTML parsing
  python3 << 'PYTHON' "$html_file" "$ENUMS_RAW"
import sys
import re

html_file = sys.argv[1]
output_file = sys.argv[2]

with open(html_file, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# Find the article tag with class "content-container markdown"
# Handle both id="" and without id
pattern = r'<article[^>]*class="content-container\s+markdown"[^>]*>(.*?)</article>'
match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)

if not match:
    # Try alternative patterns
    pattern = r'<article[^>]*class="[^"]*content-container[^"]*markdown[^"]*"[^>]*>(.*?)</article>'
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)

if not match:
    # Fallback: look for any article with markdown class
    pattern = r'<article[^>]*markdown[^>]*>(.*?)</article>'
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)

if match:
    article_content = match.group(1)
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(article_content)
    print(f"EXTRACTED:{len(article_content)}")
else:
    # If no article found, try to extract the main content area
    # Sometimes the page uses a different structure
    pattern = r'<main[^>]*>(.*?)</main>'
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
    if match:
        article_content = match.group(1)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(article_content)
        print(f"EXTRACTED_MAIN:{len(article_content)}")
    else:
        print("NO_MATCH")
        sys.exit(1)
PYTHON
}

parse_enums_to_json() {
  local html_file="$1"

  ENUM_HTML_FILE="$html_file" python3 << 'PYTHON'
import sys
import re
import json
import os

def parse_table(table_html):
    """Parse an HTML table into a list of dicts."""
    values = []
    headers = []

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE)

    for row in rows:
        ths = re.findall(r'<th[^>]*>(.*?)</th>', row, re.DOTALL | re.IGNORECASE)
        if ths:
            headers = [re.sub(r'<[^>]+>', '', h).strip() for h in ths]
            continue

        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        if tds:
            cells = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
            if not cells or not cells[0]:
                continue

            entry = {"value": cells[0]}
            for idx, cell in enumerate(cells[1:], 1):
                if idx < len(headers) and cell:
                    entry[headers[idx]] = cell
            values.append(entry)

    return values


html_file = os.environ["ENUM_HTML_FILE"]

with open(html_file, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

enums = {}

sections = re.split(r'(<h[34][^>]*>.*?</h[34]>)', content, flags=re.DOTALL | re.IGNORECASE)

i = 0
while i < len(sections):
    section = sections[i]

    heading_match = re.match(r'<h[34][^>]*>(.*?)</h[34]>', section, re.DOTALL | re.IGNORECASE)
    if heading_match:
        heading_text = re.sub(r'<[^>]+>', '', heading_match.group(1)).strip()
        heading_text = re.sub(r'\s+', ' ', heading_text)

        if not heading_text or heading_text.lower() in ['enums', '']:
            i += 1
            continue

        current_enum = heading_text

        if i + 1 < len(sections):
            body = sections[i + 1]

            table_match = re.search(r'<table[^>]*>(.*?)</table>', body, re.DOTALL | re.IGNORECASE)
            if table_match:
                table_html = table_match.group(1)
                values = parse_table(table_html)
                if values:
                    enums[current_enum] = {
                        "values": values,
                        "count": len(values)
                    }

            desc_match = re.search(r'<strong>Description</strong>\s*:\s*(.*?)</p>', body, re.DOTALL | re.IGNORECASE)
            if desc_match and current_enum in enums:
                desc_text = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()
                enums[current_enum]["description"] = desc_text

            ref_match = re.search(r'<strong>Referenced by</strong>\s*:\s*(.*?)</p>', body, re.DOTALL | re.IGNORECASE)
            if ref_match and current_enum in enums:
                ref_text = re.sub(r'<[^>]+>', '', ref_match.group(1)).strip()
                enums[current_enum]["referencedBy"] = ref_text

    i += 1

result = {
    "source": "https://advertising.amazon.com/API/docs/en-us/reference/common-models/enums",
    "extractedAt": None,
    "enumCount": len(enums),
    "enums": enums
}

print(json.dumps(result, indent=2, ensure_ascii=False))
PYTHON
}

parse_enums_to_markdown() {
  local json_file="$1"

  # Convert JSON enums to readable markdown
  python3 << 'PYTHON' "$json_file"
import sys
import json

json_file = sys.argv[1]

with open(json_file, 'r') as f:
    data = json.load(f)

print(f"# Amazon Ads API — Enum Reference")
print(f"")
print(f"> Source: {data['source']}")
print(f"> Extracted: {data.get('extractedAt', 'unknown')}")
print(f"> Total enums: {data['enumCount']}")
print(f"")
print(f"---")
print(f"")

for name, info in sorted(data.get('enums', {}).items()):
    print(f"## {name}")
    print(f"")
    if 'description' in info:
        print(f"{info['description']}")
        print(f"")

    values = info.get('values', [])
    if values:
        # Check if any have descriptions
        has_desc = any(v.get('description') for v in values)
        has_extra = any(v.get('extra') for v in values)

        if has_extra:
            print(f"| Value | Description | Notes |")
            print(f"|-------|-------------|-------|")
            for v in values:
                print(f"| `{v['value']}` | {v.get('description', '')} | {v.get('extra', '')} |")
        elif has_desc:
            print(f"| Value | Description |")
            print(f"|-------|-------------|")
            for v in values:
                print(f"| `{v['value']}` | {v.get('description', '')} |")
        else:
            for v in values:
                print(f"- `{v['value']}`")
    print(f"")

PYTHON
}

compare_enums() {
  if [[ ! -f "$ENUMS_PREVIOUS" ]]; then
    log_warn "No previous enum snapshot found (first run)"
    return 2
  fi

  if diff -q "$ENUMS_PREVIOUS" "$ENUMS_JSON" &>/dev/null; then
    log_ok "No enum changes detected"
    return 0
  fi

  return 1
}

show_enum_changes() {
  echo ""
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${YELLOW}  ENUM CHANGES DETECTED${NC}"
  echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""

  # Compare enum counts
  local prev_count curr_count
  prev_count=$(jq '.enumCount' "$ENUMS_PREVIOUS" 2>/dev/null || echo 0)
  curr_count=$(jq '.enumCount' "$ENUMS_JSON" 2>/dev/null || echo 0)
  echo "  Enum count: $prev_count → $curr_count"
  echo ""

  # Find new enums
  local new_enums
  new_enums=$(diff <(jq -r '.enums | keys[]' "$ENUMS_PREVIOUS" 2>/dev/null | sort) \
                   <(jq -r '.enums | keys[]' "$ENUMS_JSON" 2>/dev/null | sort) \
              | grep '^>' | sed 's/^> //' || true)
  if [[ -n "$new_enums" ]]; then
    echo -e "  ${GREEN}New enums:${NC}"
    echo "$new_enums" | sed 's/^/    + /'
    echo ""
  fi

  # Find removed enums
  local removed_enums
  removed_enums=$(diff <(jq -r '.enums | keys[]' "$ENUMS_PREVIOUS" 2>/dev/null | sort) \
                       <(jq -r '.enums | keys[]' "$ENUMS_JSON" 2>/dev/null | sort) \
                  | grep '^<' | sed 's/^< //' || true)
  if [[ -n "$removed_enums" ]]; then
    echo -e "  ${RED}Removed enums:${NC}"
    echo "$removed_enums" | sed 's/^/    - /'
    echo ""
  fi

  # For each shared enum, check for value changes
  echo -e "  ${CYAN}Value changes per enum:${NC}"
  local shared_enums
  shared_enums=$(comm -12 <(jq -r '.enums | keys[]' "$ENUMS_PREVIOUS" 2>/dev/null | sort) \
                          <(jq -r '.enums | keys[]' "$ENUMS_JSON" 2>/dev/null | sort) || true)

  local changed=0
  while IFS= read -r enum_name; do
    [[ -z "$enum_name" ]] && continue

    local prev_values curr_values
    prev_values=$(jq -r --arg name "$enum_name" '.enums[$name].values[]?.value // empty' "$ENUMS_PREVIOUS" 2>/dev/null | sort)
    curr_values=$(jq -r --arg name "$enum_name" '.enums[$name].values[]?.value // empty' "$ENUMS_JSON" 2>/dev/null | sort)

    local added removed
    added=$(diff <(echo "$prev_values") <(echo "$curr_values") | grep '^>' | sed 's/^> //' || true)
    removed=$(diff <(echo "$prev_values") <(echo "$curr_values") | grep '^<' | sed 's/^< //' || true)

    if [[ -n "$added" || -n "$removed" ]]; then
      ((changed++))
      echo "    📋 $enum_name:"
      if [[ -n "$added" ]]; then
        echo "$added" | sed 's/^/      + /'
      fi
      if [[ -n "$removed" ]]; then
        echo "$removed" | sed 's/^/      - /'
      fi
    fi
  done <<< "$shared_enums"

  if [[ "$changed" -eq 0 ]]; then
    echo "    (enum names changed but individual values unchanged — check structure)"
  fi

  echo ""
  echo -e "  ${RED}⚠️  Action required:${NC}"
  echo "    1. Review enum changes above"
  echo "    2. Ask Kiro: \"Update migration skills from the latest diff\""
  echo "       (uses the update-migration-skills skill)"
  echo ""

  # Save old/new to diff/ directory
  local diff_dir="$ENUMS_DIR/diff"
  mkdir -p "$diff_dir"
  cp "$ENUMS_PREVIOUS" "$diff_dir/enums-unified-api.old.json"
  cp "$ENUMS_JSON" "$diff_dir/enums-unified-api.new.json"
  log_ok "Saved diff: api-specs/diff/enums-unified-api.{old,new}.json"
}

# --- Main --------------------------------------------------------------------

main() {
  local mode="normal"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check)    mode="check" ;;
      --raw)      mode="raw" ;;
      --markdown) mode="markdown" ;;
      --help|-h)
        echo "Usage: $0 [--check|--raw|--markdown|--help]"
        echo ""
        echo "Options:"
        echo "  --check      CI mode: exit 1 if enums have changed"
        echo "  --raw        Output raw extracted HTML (for debugging)"
        echo "  --markdown   Output parsed enums as markdown"
        echo "  --help       Show this help"
        echo ""
        echo "Source: $ENUMS_URL"
        exit 0
        ;;
      *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
  done

  echo ""
  echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BLUE}║  Amazon Ads API — Enum Extractor                            ║${NC}"
  echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo ""

  check_dependencies
  mkdir -p "$ENUMS_DIR"

  # Step 1: Download page
  local html_file
  html_file=$(download_page)
  if [[ $? -ne 0 || -z "$html_file" ]]; then
    log_error "Download failed"
    exit 1
  fi

  # Step 2: Extract article content
  log_info "Extracting <article class='content-container markdown'> content..."
  local extract_result
  extract_result=$(extract_article "$html_file" 2>&1) || true

  if [[ "$extract_result" == "NO_MATCH" ]]; then
    log_warn "Could not find <article> tag. Page may require JavaScript rendering."
    log_warn "Attempting to extract from full page body as fallback..."

    # Fallback: try to find any table content in the page
    cp "$html_file" "$ENUMS_RAW"
    extract_result="FALLBACK"
  fi

  log_ok "Article content extracted"

  # --raw mode: just output the HTML
  if [[ "$mode" == "raw" ]]; then
    if [[ -f "$ENUMS_RAW" ]]; then
      cat "$ENUMS_RAW"
    else
      log_error "No raw content available"
    fi
    rm -f "$html_file"
    exit 0
  fi

  # Step 3: Save previous version for comparison
  if [[ -f "$ENUMS_JSON" ]]; then
    cp "$ENUMS_JSON" "$ENUMS_PREVIOUS"
  fi

  # Step 4: Parse to JSON
  log_info "Parsing enum tables to JSON..."
  local json_output
  json_output=$(parse_enums_to_json "$ENUMS_RAW" 2>/dev/null) || true

  if [[ -z "$json_output" || "$json_output" == "null" ]]; then
    log_warn "No enums parsed from curl-fetched HTML (page is JS-rendered)."
    log_info "Attempting headless browser extraction..."

    local manual_html="$ENUMS_DIR/enums-raw-manual.html"
    local fetch_success=false

    # Use Python + Playwright to render the JS page and extract the article
    local fetch_script="$SCRIPT_DIR/fetch-enums-page.py"
    if [[ -f "$fetch_script" ]] && python3 -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
      log_info "  Using Python Playwright (headless Chromium)..."
      if python3 "$fetch_script" "$manual_html" 2>&1 | while read -r line; do echo "    $line"; done; then
        if [[ -f "$manual_html" && -s "$manual_html" ]]; then
          fetch_success=true
        fi
      fi
    else
      log_warn "  Python Playwright not available. Install with:"
      log_warn "    pip3 install playwright"
      log_warn "    python3 -m playwright install chromium"
    fi

    # Apply fetched result
    if [[ "$fetch_success" == "true" ]]; then
      log_ok "Playwright extraction succeeded"
      cp "$manual_html" "$ENUMS_RAW"
      json_output=$(parse_enums_to_json "$ENUMS_RAW" 2>/dev/null) || true
    fi

    # Still no data — try existing manual file
    if [[ -z "$json_output" || "$json_output" == "null" ]]; then
      if [[ -f "$manual_html" && -s "$manual_html" ]]; then
        log_info "Found existing manual HTML file, using it..."
        cp "$manual_html" "$ENUMS_RAW"
        json_output=$(parse_enums_to_json "$ENUMS_RAW" 2>/dev/null) || true
      fi
    fi

    if [[ -z "$json_output" || "$json_output" == "null" ]]; then
      log_warn ""
      log_warn "All extraction methods failed. Install Playwright:"
      log_warn "  pip3 install playwright"
      log_warn "  python3 -m playwright install chromium"
      log_warn "  Then re-run: $0"
      rm -f "$html_file"
      exit 1
    fi
  fi

  # Add timestamp
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  json_output=$(echo "$json_output" | jq --arg ts "$timestamp" '.extractedAt = $ts')

  echo "$json_output" > "$ENUMS_JSON"

  local enum_count
  enum_count=$(echo "$json_output" | jq '.enumCount')
  log_ok "Extracted $enum_count enums → $ENUMS_JSON"

  # Step 5: Generate markdown
  parse_enums_to_markdown "$ENUMS_JSON" > "$ENUMS_MD"
  log_ok "Markdown generated → $ENUMS_MD"

  # --markdown mode
  if [[ "$mode" == "markdown" ]]; then
    cat "$ENUMS_MD"
    rm -f "$html_file"
    exit 0
  fi

  # Step 6: Compare with previous
  echo ""
  local compare_result
  set +e
  compare_enums
  compare_result=$?
  set -e

  case $compare_result in
    0)
      echo -e "${GREEN}┌─────────────────────────────────────────────────────────────┐${NC}"
      echo -e "${GREEN}│  ✅ Enums unchanged. No skill updates needed.               │${NC}"
      echo -e "${GREEN}└─────────────────────────────────────────────────────────────┘${NC}"
      ;;
    1)
      show_enum_changes
      if [[ "$mode" == "check" ]]; then
        rm -f "$html_file"
        exit 1
      fi
      ;;
    2)
      echo -e "${YELLOW}┌─────────────────────────────────────────────────────────────┐${NC}"
      echo -e "${YELLOW}│  📝 First run — baseline saved. Run again to detect changes.│${NC}"
      echo -e "${YELLOW}└─────────────────────────────────────────────────────────────┘${NC}"
      ;;
  esac

  # Cleanup
  rm -f "$html_file"
  echo ""
}

main "$@"
