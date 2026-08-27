#!/usr/bin/env bash
# =============================================================================
# install.sh — Install Amazon Ads API skills into a Kiro workspace
#
# Usage:
#   ./install.sh /path/to/your/project           # Install all skills
#   ./install.sh /path/to/your/project sp        # Install SP-related skills only
#   ./install.sh /path/to/your/project sb        # Install SB-related skills only
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$SCRIPT_DIR/skills"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

usage() {
  echo "Usage: $0 <project-path> [filter]"
  echo ""
  echo "Arguments:"
  echo "  project-path    Path to your project (where .kiro/ lives)"
  echo "  filter          Optional: 'sp' for SP skills, 'sb' for SB skills, 'all' (default)"
  echo ""
  echo "Examples:"
  echo "  $0 ~/my-project          # Install all skills"
  echo "  $0 ~/my-project sp       # Install SP migration skills only"
  echo "  $0 ~/my-project sb       # Install SB migration + collections skills"
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

TARGET_DIR="$1"
FILTER="${2:-all}"

if [[ ! -d "$TARGET_DIR" ]]; then
  echo -e "${RED}Error: Directory '$TARGET_DIR' does not exist.${NC}"
  exit 1
fi

KIRO_SKILLS_DIR="$TARGET_DIR/.kiro/skills"
mkdir -p "$KIRO_SKILLS_DIR"

# Determine which skills to install
declare -a SKILLS_TO_INSTALL=()

case "$FILTER" in
  all)
    SKILLS_TO_INSTALL=(unified-api-migration-guide unified-sp-migration unified-sb-migration amazon-ads-sb-collections amazon-ads-spglobal unified-dsp-cm-migration amazon-ads-dsp-creative-management unified-api-cli-testing update-migration-skills)
    ;;
  sp)
    SKILLS_TO_INSTALL=(unified-api-migration-guide unified-sp-migration amazon-ads-spglobal unified-api-cli-testing update-migration-skills)
    ;;
  sb)
    SKILLS_TO_INSTALL=(unified-api-migration-guide unified-sb-migration amazon-ads-sb-collections unified-api-cli-testing update-migration-skills)
    ;;
  *)
    echo -e "${RED}Unknown filter: $FILTER${NC}"
    echo "Valid options: all, sp, sb"
    exit 1
    ;;
esac

echo ""
echo -e "${BLUE}Installing Amazon Ads API skills...${NC}"
echo -e "  Target: $KIRO_SKILLS_DIR"
echo -e "  Filter: $FILTER (${#SKILLS_TO_INSTALL[@]} skills)"
echo ""

INSTALLED=0
for skill in "${SKILLS_TO_INSTALL[@]}"; do
  if [[ -d "$SKILLS_DIR/$skill" ]]; then
    cp -r "$SKILLS_DIR/$skill" "$KIRO_SKILLS_DIR/"
    echo -e "  ${GREEN}✓${NC} $skill"
    ((INSTALLED++))
  else
    echo -e "  ${YELLOW}⚠${NC} $skill (not found, skipping)"
  fi
done

echo ""
echo -e "${GREEN}Done! Installed $INSTALLED skills to $KIRO_SKILLS_DIR${NC}"
echo ""

# Install agent configuration
KIRO_AGENTS_DIR="$TARGET_DIR/.kiro/agents"
mkdir -p "$KIRO_AGENTS_DIR"

AGENT_SRC="$SCRIPT_DIR/.kiro/agents/ads-api-migration-assistant.json"
if [[ -f "$AGENT_SRC" ]]; then
  cp "$AGENT_SRC" "$KIRO_AGENTS_DIR/"
  echo -e "  ${GREEN}✓${NC} Agent: ads-api-migration-assistant"
else
  echo -e "  ${YELLOW}⚠${NC} Agent config not found (skipping)"
fi

# Install API spec files (for agent fallback)
API_SPECS_DST="$TARGET_DIR/api-specs"
mkdir -p "$API_SPECS_DST"

SPEC_FILES=("unified-api-sp.json" "unified-api-sb.json" "unified-api-spglobal.json" "unified-api-dsp.json" "enums-unified-api.json")
SPECS_COPIED=0
for spec in "${SPEC_FILES[@]}"; do
  if [[ -f "$SCRIPT_DIR/api-specs/$spec" ]]; then
    cp "$SCRIPT_DIR/api-specs/$spec" "$API_SPECS_DST/"
    ((SPECS_COPIED++))
  fi
done

if [[ $SPECS_COPIED -gt 0 ]]; then
  echo -e "  ${GREEN}✓${NC} API specs: $SPECS_COPIED files (for agent fallback)"
else
  echo -e "  ${YELLOW}⚠${NC} API spec files not found (skipping)"
fi

echo ""
echo "Verify by asking Kiro:"
echo '  "How do I migrate my SP v3 campaign to the Unified API?"'
echo ""
echo "The agent will use SKILL.md content first, and automatically"
echo "fall back to OpenAPI spec files (api-specs/*.json) when it needs"
echo "more detail about specific schemas, fields, or enum values."
echo ""
