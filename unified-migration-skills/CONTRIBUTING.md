# Contributing

Thank you for contributing to the Amazon Ads API Migration Skills! This guide covers how to add new skills, update existing ones, and maintain quality standards.

## Skill File Format

Every skill lives in `skills/<skill-name>/SKILL.md` and follows this structure:

```markdown
---
name: skill-name
description: "Use when [specific trigger scenario]. Including [key topics covered]."
---

# Skill Title

## 1. Overview / Context

[Background information the AI needs to understand the domain]

## 2. Core Content

[The main knowledge: API mappings, code examples, tables, etc.]

## 3. Examples / Code

[Concrete before/after examples with code blocks]

## 4. FAQ / Troubleshooting

[Common questions and error resolution]

## N. Checklist (if applicable)

[Step-by-step migration or implementation checklist]
```

### Frontmatter Rules

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Lowercase, hyphenated identifier (must match directory name) |
| `description` | Yes | Starts with "Use when..." — this tells Kiro WHEN to activate the skill |

### Description Best Practices

The `description` field is critical — it's how Kiro decides whether to use your skill. Be specific:

✅ Good: `"Use when migrating Sponsored Products campaigns from SP v3 API (/sp/) to the Unified API (/adsApi/v1/), including endpoint mapping, request body restructuring, and troubleshooting."`

❌ Bad: `"Information about Amazon Ads APIs."`

## Adding a New Skill

1. Create the directory:
   ```bash
   mkdir -p skills/my-new-skill
   ```

2. Write your SKILL.md following the template above

3. Update `skills.json`:
   ```bash
   # Add your skill entry to the manifest
   ```

4. Test by installing into a workspace:
   ```bash
   cp -r skills/my-new-skill /path/to/project/.kiro/skills/
   ```

5. Verify Kiro uses it correctly by asking a relevant question

6. Submit a PR

## Updating a Skill After API Spec Changes

When `./scripts/sync-api-specs.sh` reports changes:

1. Run the sync script to see what changed:
   ```bash
   ./scripts/sync-api-specs.sh
   ```

2. Check the specific diff:
   ```bash
   git diff api-specs/unified-api-sp.json
   ```

3. Update the affected SKILL.md:
   - New endpoints → Add to endpoint mapping tables
   - Changed fields → Update field mapping tables
   - New schemas → Add relevant documentation
   - Removed features → Update "Not Available" sections

4. Update the confidence labels:
   - ✅ Confirmed from schema
   - ⚠️ Reasonable inference
   - ❓ Unconfirmed

5. Commit together:
   ```bash
   git add api-specs/ skills/
   git commit -m "sync: update specs and skills for [change description]"
   ```

## Quality Standards

### Content Checklist

- [ ] Frontmatter with `name` and `description`
- [ ] Description starts with "Use when..."
- [ ] Tables use proper markdown formatting
- [ ] Code examples are complete and copy-pasteable
- [ ] Before/after comparisons for migration content
- [ ] Confidence labels on inferred mappings
- [ ] FAQ section addresses common errors
- [ ] Checklist for migration steps (if applicable)

### Style Guidelines

- Use tables for field mappings (easier to scan than prose)
- Include both the old and new API structure in code blocks
- Label confidence levels: ✅ confirmed, ⚠️ inferred, ❓ unconfirmed
- Keep code examples minimal but complete (show required fields)
- Use consistent header numbering within each skill

### Testing Your Changes

1. Install the updated skill in a workspace
2. Ask Kiro questions that should trigger the skill
3. Verify the answers use the correct, updated information
4. Test edge cases: "What about [feature X]?" or "How do I troubleshoot [error Y]?"

## Commit Message Format

```
<type>: <description>

Types:
  feat     — New skill or major skill section
  fix      — Correct inaccurate information
  sync     — Update from API spec changes
  docs     — README, CONTRIBUTING, etc.
  chore    — Scripts, CI, project config
```

Examples:
- `feat: add SD (Sponsored Display) migration skill`
- `fix: correct placement enum mapping for SB`
- `sync: update SP spec — new /targets/bid endpoint`
- `docs: add GitHub Actions workflow example`

## Questions?

Open an issue for:
- Questions about a mapping (tag with `question`)
- Reporting an inaccuracy (tag with `bug`)
- Requesting a new skill (tag with `enhancement`)
