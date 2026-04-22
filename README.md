# GitLab Issues to GitHub Migration

Migrate issues from a GitLab project to a GitHub repository, preserving titles, descriptions, labels, milestones, assignees, comments, and open/closed state.

## Prerequisites

- Python 3.8+
- A GitLab personal access token with `read_api` scope
- A GitHub personal access token with `repo` scope

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `GITLAB_URL` | Yes | GitLab instance URL (e.g. `https://gitlab.com`) |
| `GITLAB_TOKEN` | Yes | GitLab personal access token |
| `GITLAB_PROJECT_ID` | Yes | Numeric project ID (found in project settings) |
| `GITHUB_TOKEN` | Yes | GitHub personal access token |
| `GITHUB_REPO` | Yes | Target repo in `owner/repo` format |
| `USERNAME_MAP` | No | JSON mapping GitLab to GitHub usernames |
| `SCOPE_COLORS` | No | JSON mapping scoped label prefixes to hex colors |
| `MIGRATE_IMAGES` | No | Set to `0` to skip image migration (default: `1`) |
| `DRY_RUN` | No | Set to `1` to preview without making changes |

## Usage

Preview the migration (recommended first step):

```bash
DRY_RUN=1 python migrate.py
```

Run the migration:

```bash
python migrate.py
```

## Username Mapping

GitLab and GitHub usernames often differ. Set `USERNAME_MAP` to translate them:

```
USERNAME_MAP={"john.doe": "johndoe", "jane": "jdoe-gh"}
```

Unmapped usernames are wrapped in backticks in issue bodies to prevent false GitHub notifications.

## Idempotency

The script tracks migrated issues in `.migration_state.json`. If the migration is interrupted, re-run the script — already-migrated issues will be skipped automatically.

## Scoped Label Colors

GitLab scoped labels (e.g. `x-priority :: P1`, `x-team :: workforce-sales`) are migrated as GitHub labels with consistent colors per scope. Default colors:

| Scope | Color | Hex |
|---|---|---|
| `x-priority` | Red | `d93f0b` |
| `x-type` | Blue | `0075ca` |
| `x-team` | Green | `0e8a16` |
| `x-workflow` | Purple | `5319e7` |
| `status` | Yellow | `fbca04` |

Override or extend with `SCOPE_COLORS`:

```
SCOPE_COLORS={"x-priority": "e11d48", "Enterprise": "f97316"}
```

If the GitLab label already has a non-default color, that color is preserved.

## GitLab to GitHub Feature Mapping

| GitLab | GitHub | Notes |
|---|---|---|
| Issues | Issues | Full migration with metadata |
| Scoped labels (`x-priority :: P1`) | Labels (color-grouped) | Visually organized by scope color |
| Milestones | Milestones | Title, description, due date, state |
| Issue comments | Issue comments | Author attribution preserved |
| System notes | Issue comments (marked) | Label changes, assignments, etc. |
| Award emoji | Reactions | Mapped to GitHub's supported set |
| Issue links | Cross-references in body | Blocks, relates to, etc. |
| Linked MRs | Links in body | With URL, state, and title |
| Due date, weight, time tracking | Metadata table in body | GitHub has no native fields for these |
| Confidential issues | Noted in metadata table | GitHub has no confidential flag |
| Assignees | Assignees | Must be repo collaborators |
| Issue state (open/closed) | Issue state | Including closed-by info |
| Images & file attachments | Uploaded to repo | Stored in `.github/migration-assets/`, URLs rewritten |

## Image Migration

By default (`MIGRATE_IMAGES=1`), images and file attachments embedded in GitLab issues and comments are:

1. **Downloaded** from GitLab using the authenticated API
2. **Uploaded** to the GitHub repo under `.github/migration-assets/{hash}/{filename}`
3. **URL-rewritten** in the issue/comment markdown so they render on GitHub

This ensures images remain accessible even if the GitLab instance goes offline or is private. Duplicate uploads (same image in multiple issues) are cached and only uploaded once.

Set `MIGRATE_IMAGES=0` to skip this and use absolute GitLab URLs instead (images will break if GitLab is private or removed).

## Limitations
- **Assignees**: GitHub assignees must be collaborators on the target repository. Unrecognized assignees are silently ignored by the GitHub API.
- **Issue numbers**: GitHub issue numbers won't match GitLab IIDs. Each migrated issue includes the original GitLab issue number in its header.
- **Comments**: All comments are created by the token owner. Original author attribution is preserved in the comment body.
