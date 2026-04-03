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

## Limitations

- **Images**: GitLab upload paths are converted to absolute URLs. If the GitLab project is private, images won't render for unauthenticated users.
- **Assignees**: GitHub assignees must be collaborators on the target repository. Unrecognized assignees are silently ignored by the GitHub API.
- **Issue numbers**: GitHub issue numbers won't match GitLab IIDs. Each migrated issue includes the original GitLab issue number in its header.
- **Comments**: All comments are created by the token owner. Original author attribution is preserved in the comment body.
