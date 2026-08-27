---
name: update-migration-skills
description: "Use when API specs have changed and the developer asks to update the migration skills based on the diffs in api-specs/diff/. This skill reads old vs new OpenAPI spec diffs and updates the unified-sp-migration, unified-sb-migration, and related SKILL.md files accordingly."
---

# Update Migration Skills from API Spec Diffs

## 1. When to Use This Skill

Use this skill when:
- The developer ran `./scripts/sync-api-specs.sh` and it detected changes
- The developer ran `./scripts/extract-enums.sh` and it detected enum changes
- The developer asks: "Update migration skills from the latest diff"
- Files exist in `api-specs/diff/` directory

## 2. Workflow

```
1. Read api-specs/diff/*.changes.md   → understand WHAT changed
2. Read api-specs/diff/*.old.json     → understand the OLD spec
3. Read api-specs/diff/*.new.json     → understand the NEW spec
4. Determine which SKILL.md files are affected
5. Update the affected sections in each SKILL.md
6. Report what was changed
```

## 3. File Locations

| File | Purpose |
|------|---------|
| `api-specs/diff/unified-api-sp.old.json` | Previous SP OpenAPI spec |
| `api-specs/diff/unified-api-sp.new.json` | Current SP OpenAPI spec |
| `api-specs/diff/unified-api-sp.changes.md` | Summary of SP changes |
| `api-specs/diff/unified-api-sb.old.json` | Previous SB OpenAPI spec |
| `api-specs/diff/unified-api-sb.new.json` | Current SB OpenAPI spec |
| `api-specs/diff/unified-api-sb.changes.md` | Summary of SB changes |
| `api-specs/diff/enums-unified-api.old.json` | Previous enum values |
| `api-specs/diff/enums-unified-api.new.json` | Current enum values |

## 4. Which Skills to Update

| Diff Source | Affected Skills |
|-------------|-----------------|
| `unified-api-sp.*.json` | `skills/unified-sp-migration/SKILL.md` |
| `unified-api-sb.*.json` | `skills/unified-sb-migration/SKILL.md`, `skills/amazon-ads-sb-collections/SKILL.md` |
| `enums-unified-api.*.json` | ALL migration skills (enum mapping tables) |

## 5. Update Rules

### 5.1 New Endpoints Added

When `*.changes.md` shows new endpoints:

1. **Find the matching section** in the SKILL.md endpoint mapping table (Section 3)
2. **Add a new row** to the endpoint mapping table
3. **Check the request schema** in the new spec to determine the request body structure
4. If the endpoint is for a resource already covered (e.g., new CRUD operation for existing resource):
   - Add to the existing endpoint table
5. If the endpoint is for an entirely new resource type:
   - Add a new section with the full create/query/update/delete mapping
   - Include a request body example

**Template for new endpoint row:**
```markdown
| [Operation Name] | [Legacy path or "N/A (new)"] | `POST /adsApi/v1/{action}/{resource}` |
```

### 5.2 Removed Endpoints

When endpoints are removed from the spec:

1. **Do NOT remove them from the SKILL.md** immediately
2. **Mark them as deprecated** with a note:
   ```markdown
   | Operation | Legacy | Unified API | Note |
   |-----------|--------|-------------|------|
   | ... | ... | ~~`POST /adsApi/v1/...`~~ | ⚠️ Removed in spec version YYYY-MM-DD |
   ```
3. **Move to "Features NOT Available" section** if confirmed permanently removed

### 5.3 Schema/Field Changes

When schemas change (new fields, removed fields, type changes):

1. **Read the specific schema** from both old and new specs:
   - `jq '.components.schemas.SchemaName' api-specs/diff/{name}.old.json`
   - `jq '.components.schemas.SchemaName' api-specs/diff/{name}.new.json`
2. **Compare field lists**:
   - New required fields → Add to field mapping table + migration checklist
   - Removed fields → Note in FAQ/troubleshooting
   - Changed types → Update the field mapping table
3. **Update code examples** if the request body structure changed

### 5.4 Enum Changes

When `enums-unified-api.new.json` differs from `enums-unified-api.old.json`:

1. **Identify which enums changed** by comparing `.enums[enumName].values`
2. **Find all references** in SKILL.md files to that enum (search for the enum name)
3. **Update mapping tables**:
   - New values → Add row to mapping table, mark as ✅ Confirmed
   - Removed values → Strike through and add deprecation note
   - Renamed values → Update the mapping and add migration note

**Enum sections to check in each skill:**
- `unified-sp-migration`: Section 4.3 (bidStrategy), 4.4 (placement), 7.3-7.6 (match types, target types)
- `unified-sb-migration`: Section 5.2 (bidStrategy), 5.3 (placement)
- `unified-api-migration-guide`: Step 4 (Quick Reference enum tables)

### 5.5 New Schemas (Models)

When new schemas appear in the spec:

1. **Assess relevance**: Is this schema used in any existing endpoint's request/response?
2. **If relevant to migration**: Add field mapping documentation
3. **If it's a new feature**: Consider adding to "Features NOT Available" section removal + new section

## 6. Confidence Labels

When adding new information to the SKILL.md, always apply confidence labels:

| Label | When to Use |
|-------|-------------|
| ✅ **Confirmed from schema** | Field name, type, enum value directly visible in the spec |
| ⚠️ **Reasonable inference** | Semantic mapping based on naming patterns (e.g., old `PRODUCT_LIST` → new `ASIN_LIST`) |
| ❓ **Unconfirmed** | Cannot determine mapping from schema alone |

**Rule**: New information derived purely from comparing spec field names/types/enums = ✅ Confirmed.
Anything requiring interpretation of business logic = ⚠️ or ❓.

## 7. Migration Checklist Updates

After updating field mapping tables, also update the Migration Checklist section at the end of each SKILL.md:

- New required field → Add checklist item: `- [ ] Add {field} to {operation} requests`
- Changed enum → Add checklist item: `- [ ] Update {enumName} values ({old} → {new})`
- New endpoint → Add checklist item: `- [ ] Implement {operation} using new endpoint`

## 8. Code Example Updates

If a request body schema changed significantly:

1. **Read the new schema** to understand current required fields
2. **Update the "Side by Side" comparison** in the relevant section
3. **Keep the old (legacy) example as-is** — only update the Unified API example
4. **Ensure minimum required fields** are correct in Quick Reference section

## 9. Validation After Updates

After updating SKILL.md files, verify:

1. All markdown tables are well-formed (equal columns per row)
2. Code examples contain valid JSON
3. No broken section references
4. Confidence labels are present on new mappings
5. Migration checklist is consistent with field mapping tables

## 10. Example: Processing a Diff

Given `api-specs/diff/unified-api-sp.changes.md`:
```
## New Endpoints
/adsApi/v1/create/adExtensions
/adsApi/v1/query/adExtensions

## New Schemas
SPCreateAdExtension
SPAdExtension
SPAdExtensionType
```

**Actions:**
1. Read the new schema: `jq '.components.schemas.SPCreateAdExtension' api-specs/diff/unified-api-sp.new.json`
2. Determine if this is a new resource type → Yes (adExtensions)
3. Add to `unified-sp-migration/SKILL.md` Section 3:
   ```markdown
   | Create Ad Extension | N/A (new in Unified API) | `POST /adsApi/v1/create/adExtensions` |
   | Query Ad Extensions | N/A (new in Unified API) | `POST /adsApi/v1/query/adExtensions` |
   ```
4. Note in "Features NOT Available in Unified API" section → remove if it was listed there
5. Optionally add a new sub-section documenting the request body structure

## 11. What NOT to Change

- **Do NOT modify legacy API examples** (the "before" code blocks)
- **Do NOT remove ❓ Unconfirmed items** unless the new spec explicitly clarifies them
- **Do NOT change the overall document structure** (section numbering, heading levels)
- **Do NOT add features from a different ad product** (SP changes → only update SP skill)
