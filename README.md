# GitLab Issues to GitHub Migration

A Python script to migrate issues from a GitLab project to a GitHub repository, preserving all issue data including labels, milestones, assignees, comments, reactions, images, and metadata.

## Features

- **Full issue migration** — titles, descriptions, labels, milestones, assignees, state (open/closed)
- **Comments** — all user comments and system notes with original author attribution
- **Labels** — preserved with original colors; scoped labels get consistent color grouping
- **Milestones** — title, description, due date, and state
- **Reactions** — GitLab award emoji mapped to GitHub reactions
- **Images & attachments** — downloaded from GitLab and re-uploaded to the GitHub repo
- **Metadata** — due dates, weights, time tracking, confidential flags, closed-by info
- **Linked issues & MRs** — cross-references preserved in the issue body
- **Idempotent** — safe to re-run after interruptions; tracks progress in a state file
- **Dry-run mode** — preview everything before making changes
- **Rate-limit aware** — automatically pauses and retries when GitHub limits are hit

## Prerequisites

- Python 3.8+
- A **GitLab** personal access token with `read_api` scope
- A **GitHub** personal access token with `repo` scope

### How to get your tokens

**GitLab token:**
1. Go to GitLab > Settings > Access Tokens
2. Create a token with `read_api` scope
3. Copy the token value

**GitHub token:**
1. Go to GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens
2. Create a token with **Read and Write** access to: Contents, Issues
3. Copy the token value

**GitLab Project ID:**
- Go to your GitLab project > Settings > General — the Project ID is displayed at the top

## Setup

1. Clone this repository:

```bash
git clone https://github.com/aikkian/gitlab-issues-to-github.git
cd gitlab-issues-to-github
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your configuration:

```bash
cp .env.example .env
```

4. Edit `.env` and fill in your values:

```env
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_PROJECT_ID=12345
GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
GITHUB_REPO=your-org/your-repo
```

## Configuration

| Variable | Required | Description |
|---|---|---|
| `GITLAB_URL` | Yes | GitLab instance URL (e.g. `https://gitlab.com`) |
| `GITLAB_TOKEN` | Yes | GitLab personal access token with `read_api` scope |
| `GITLAB_PROJECT_ID` | Yes | Numeric project ID (found in project settings) |
| `GITHUB_TOKEN` | Yes | GitHub personal access token with `repo` scope |
| `GITHUB_REPO` | Yes | Target repo in `owner/repo` format |
| `USERNAME_MAP` | No | JSON mapping GitLab to GitHub usernames |
| `SCOPE_COLORS` | No | JSON mapping scoped label prefixes to hex colors |
| `MIGRATE_IMAGES` | No | Set to `0` to skip image migration (default: `1`) |
| `DRY_RUN` | No | Set to `1` to preview without making changes |

## Usage

### Step 1: Preview (recommended)

```bash
DRY_RUN=1 python migrate.py
```

This shows what would be created without making any changes on GitHub.

### Step 2: Run the migration

```bash
python migrate.py
```

The script will log progress for each issue:

```
2024-03-15 10:30:01 INFO GitLab project: my-group/my-project
2024-03-15 10:30:02 INFO Fetching GitLab labels...
2024-03-15 10:30:02 INFO Found 25 GitLab labels
2024-03-15 10:30:03 INFO Syncing milestones...
2024-03-15 10:30:03 INFO Milestones synced: 3
2024-03-15 10:30:04 INFO Migrating issue #1: Fix login bug
2024-03-15 10:30:05 INFO   Added 4 comments
2024-03-15 10:30:06 INFO   -> GitHub issue #1 created
...
2024-03-15 10:45:00 INFO === Migration complete ===
2024-03-15 10:45:00 INFO Migrated: 607 | Skipped: 0 | Errors: 0
```

### Step 3: Verify

Check your GitHub repository to confirm issues, labels, milestones, and comments look correct.

## Username Mapping

GitLab and GitHub usernames often differ. Map them with `USERNAME_MAP`:

```env
USERNAME_MAP={"john.doe": "johndoe", "jane": "jdoe-gh"}
```

- Mapped usernames: `@john.doe` in GitLab becomes `@johndoe` on GitHub
- Unmapped usernames: wrapped in backticks (`` `@john.doe` ``) to prevent false GitHub notifications

## Image Migration

By default, images and file attachments in GitLab issues and comments are:

1. **Downloaded** from GitLab using the authenticated API
2. **Uploaded** to the GitHub repo under `.github/migration-assets/{hash}/{filename}`
3. **URL-rewritten** in markdown so they render correctly on GitHub

This ensures images work even if the GitLab instance is private or goes offline. Duplicate uploads across issues are cached and only uploaded once.

Set `MIGRATE_IMAGES=0` to skip image migration and use absolute GitLab URLs instead.

## Scoped Label Colors

GitLab scoped labels (e.g. `x-priority :: P1`, `x-team :: workforce-sales`) are migrated with consistent colors per scope prefix:

| Scope | Color | Hex |
|---|---|---|
| `x-priority` | Red | `d93f0b` |
| `x-type` | Blue | `0075ca` |
| `x-team` | Green | `0e8a16` |
| `x-workflow` | Purple | `5319e7` |
| `status` | Yellow | `fbca04` |

Override or add custom scope colors:

```env
SCOPE_COLORS={"x-priority": "e11d48", "Enterprise": "f97316"}
```

If the GitLab label already has a non-default color, that original color is preserved.

## Idempotency

The script tracks migrated issues in `.migration_state.json`. If the migration is interrupted (network error, rate limit, crash), simply re-run the script — already-migrated issues are skipped automatically.

## GitLab to GitHub Feature Mapping

| GitLab | GitHub | How |
|---|---|---|
| Issues | Issues | Full migration with metadata |
| Scoped labels | Labels (color-grouped) | Consistent color per scope prefix |
| Milestones | Milestones | Title, description, due date, state |
| Issue comments | Issue comments | Author attribution in header |
| System notes | Issue comments (marked) | Label changes, assignments, etc. |
| Award emoji | Reactions | Mapped to GitHub's supported emoji set |
| Issue links | Cross-references in body | Blocks, relates to, is blocked by |
| Linked merge requests | Links in body | With URL, state, and title |
| Due date, weight, time tracking | Metadata table in body | GitHub has no native fields |
| Confidential issues | Noted in metadata table | GitHub has no confidential flag |
| Assignees | Assignees | Mapped via USERNAME_MAP |
| Issue state (open/closed) | Issue state | Including closed-by and closed-at info |
| Images & file attachments | Uploaded to repo | `.github/migration-assets/`, URLs rewritten |

## Limitations

- **Assignees** must be collaborators on the GitHub repository. Unrecognized assignees are silently ignored by the GitHub API.
- **Issue numbers** on GitHub won't match GitLab IIDs. Each migrated issue includes the original GitLab issue number in its header for reference.
- **Comments** are created by the GitHub token owner. Original author name and username are preserved in each comment's header.
- **Reactions** — GitHub supports only 8 reaction types (+1, -1, laugh, confused, heart, hooray, rocket, eyes). GitLab emoji that don't map are skipped.

## License

MIT
