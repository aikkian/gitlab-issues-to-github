#!/usr/bin/env python3
"""Migrate GitLab issues to GitHub, preserving labels, milestones, comments, and state."""

import json
import logging
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITLAB_URL = os.getenv("GITLAB_URL", "").rstrip("/")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
GITLAB_PROJECT_ID = os.getenv("GITLAB_PROJECT_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
USERNAME_MAP = json.loads(os.getenv("USERNAME_MAP", "{}"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
STATE_FILE = os.getenv("MIGRATION_STATE_FILE", ".migration_state.json")


def validate_config():
    missing = []
    for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT_ID", "GITHUB_TOKEN", "GITHUB_REPO"):
        if not globals()[name]:
            missing.append(name)
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        log.error("Copy .env.example to .env and fill in the values.")
        sys.exit(1)
    if "/" not in GITHUB_REPO:
        log.error("GITHUB_REPO must be in 'owner/repo' format, got: %s", GITHUB_REPO)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Migration state (idempotency)
# ---------------------------------------------------------------------------


class MigrationState:
    """Tracks which GitLab issues have already been migrated."""

    def __init__(self, filepath):
        self.filepath = filepath
        if os.path.exists(filepath):
            with open(filepath) as f:
                self.data = json.load(f)
        else:
            self.data = {}

    def is_migrated(self, gitlab_iid):
        return str(gitlab_iid) in self.data

    def record(self, gitlab_iid, github_issue_number):
        self.data[str(gitlab_iid)] = github_issue_number
        with open(self.filepath, "w") as f:
            json.dump(self.data, f, indent=2)


# ---------------------------------------------------------------------------
# GitLab client
# ---------------------------------------------------------------------------


class GitLabClient:
    def __init__(self, url, token, project_id):
        self.base_url = f"{url}/api/v4"
        self.project_id = project_id
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = token

    def _get_paginated(self, endpoint, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while True:
            params["page"] = page
            resp = self.session.get(f"{self.base_url}{endpoint}", params=params)
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break
            yield from items
            next_page = resp.headers.get("X-Next-Page", "")
            if not next_page:
                break
            page = int(next_page)

    def get_issues(self):
        endpoint = f"/projects/{self.project_id}/issues"
        yield from self._get_paginated(endpoint, {"state": "all", "sort": "asc", "order_by": "created_at"})

    def get_issue_notes(self, iid):
        endpoint = f"/projects/{self.project_id}/issues/{iid}/notes"
        notes = list(self._get_paginated(endpoint, {"sort": "asc", "order_by": "created_at"}))
        return [n for n in notes if not n.get("system", False)]

    def get_milestones(self):
        endpoint = f"/projects/{self.project_id}/milestones"
        return list(self._get_paginated(endpoint, {"state": "all"}))

    def get_project(self):
        resp = self.session.get(f"{self.base_url}/projects/{self.project_id}")
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------


class GitHubClient:
    API = "https://api.github.com"

    def __init__(self, token, repo):
        self.owner, self.repo = repo.split("/", 1)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        })
        self._label_cache = set()
        self._milestone_cache = {}  # title -> number

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.API}{endpoint}"
        while True:
            resp = self.session.request(method, url, **kwargs)
            # Handle primary rate limit
            if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                reset_at = int(resp.headers.get("X-RateLimit-Reset", 0))
                wait = max(reset_at - int(time.time()), 1)
                log.warning("GitHub rate limit hit. Sleeping %d seconds...", wait)
                time.sleep(wait)
                continue
            # Handle secondary (abuse) rate limit
            if resp.status_code == 403 and "Retry-After" in resp.headers:
                wait = int(resp.headers["Retry-After"])
                log.warning("GitHub secondary rate limit. Sleeping %d seconds...", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 422:
                log.error("GitHub validation error: %s", resp.text)
            resp.raise_for_status()
            # Throttle writes to avoid abuse detection
            if method.upper() in ("POST", "PATCH", "PUT"):
                time.sleep(1)
            return resp

    def ensure_label(self, name, color=None):
        if name in self._label_cache:
            return
        endpoint = f"/repos/{self.owner}/{self.repo}/labels/{requests.utils.quote(name, safe='')}"
        check = self.session.get(f"{self.API}{endpoint}")
        if check.status_code == 200:
            self._label_cache.add(name)
            return
        if DRY_RUN:
            log.info("[DRY RUN] Would create label: %s", name)
            self._label_cache.add(name)
            return
        gh_color = (color or "#ededed").lstrip("#")
        self._request("POST", f"/repos/{self.owner}/{self.repo}/labels",
                       json={"name": name, "color": gh_color})
        self._label_cache.add(name)
        log.info("Created label: %s", name)

    def ensure_milestone(self, title, description=None, due_on=None, state=None):
        if title in self._milestone_cache:
            return self._milestone_cache[title]
        # Fetch existing milestones
        if not self._milestone_cache:
            page = 1
            while True:
                resp = self.session.get(
                    f"{self.API}/repos/{self.owner}/{self.repo}/milestones",
                    params={"state": "all", "per_page": 100, "page": page},
                )
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    break
                for m in items:
                    self._milestone_cache[m["title"]] = m["number"]
                page += 1
        if title in self._milestone_cache:
            return self._milestone_cache[title]
        if DRY_RUN:
            log.info("[DRY RUN] Would create milestone: %s", title)
            self._milestone_cache[title] = -1
            return -1
        payload = {"title": title}
        if description:
            payload["description"] = description
        if due_on:
            payload["due_on"] = due_on
        resp = self._request("POST", f"/repos/{self.owner}/{self.repo}/milestones", json=payload)
        number = resp.json()["number"]
        self._milestone_cache[title] = number
        log.info("Created milestone: %s (#%d)", title, number)
        if state == "closed":
            self._request("PATCH", f"/repos/{self.owner}/{self.repo}/milestones/{number}",
                           json={"state": "closed"})
        return number

    def create_issue(self, title, body, labels=None, milestone_number=None, assignees=None):
        if DRY_RUN:
            log.info("[DRY RUN] Would create issue: %s", title)
            return -1
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if milestone_number and milestone_number > 0:
            payload["milestone"] = milestone_number
        if assignees:
            payload["assignees"] = assignees
        resp = self._request("POST", f"/repos/{self.owner}/{self.repo}/issues", json=payload)
        return resp.json()["number"]

    def add_comment(self, issue_number, body):
        if DRY_RUN:
            log.info("[DRY RUN] Would add comment to issue #%d", issue_number)
            return
        self._request("POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                       json={"body": body})

    def close_issue(self, issue_number):
        if DRY_RUN:
            log.info("[DRY RUN] Would close issue #%d", issue_number)
            return
        self._request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                       json={"state": "closed"})


# ---------------------------------------------------------------------------
# Content transformation
# ---------------------------------------------------------------------------


def map_username(match):
    gl_user = match.group(1)
    gh_user = USERNAME_MAP.get(gl_user)
    if gh_user:
        return f"@{gh_user}"
    return f"`@{gl_user}`"


def convert_body(body, gitlab_project_path=""):
    if not body:
        return ""
    # Map @username references
    body = re.sub(r"@(\w+)", map_username, body)
    # Convert relative GitLab upload paths to absolute URLs
    if gitlab_project_path:
        body = re.sub(
            r"(/uploads/[a-f0-9]{32}/[^\s)]+)",
            rf"{GITLAB_URL}/{gitlab_project_path}\1",
            body,
        )
    return body


def format_issue_body(issue, gitlab_project_path=""):
    author = issue.get("author", {})
    author_name = author.get("name", "Unknown")
    author_username = author.get("username", "unknown")
    created_at = issue.get("created_at", "")[:10]

    header = (
        f"> *Migrated from GitLab issue #{issue['iid']}, "
        f"originally created by **{author_name}** (`@{author_username}`) on {created_at}*\n\n"
    )
    body = convert_body(issue.get("description", ""), gitlab_project_path)
    return header + body


def format_comment(note, gitlab_project_path=""):
    author = note.get("author", {})
    author_name = author.get("name", "Unknown")
    author_username = author.get("username", "unknown")
    created_at = note.get("created_at", "")[:10]

    header = (
        f"> **{author_name}** (`@{author_username}`) commented on {created_at}:\n\n"
    )
    body = convert_body(note.get("body", ""), gitlab_project_path)
    return header + body


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------


def migrate():
    validate_config()

    if DRY_RUN:
        log.info("=== DRY RUN MODE — no changes will be made on GitHub ===")

    gitlab = GitLabClient(GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECT_ID)
    github = GitHubClient(GITHUB_TOKEN, GITHUB_REPO)
    state = MigrationState(STATE_FILE)

    # Get GitLab project path for image URL conversion
    project = gitlab.get_project()
    project_path = project.get("path_with_namespace", "")
    log.info("GitLab project: %s", project_path)

    # --- Pre-create milestones ---
    log.info("Syncing milestones...")
    gl_milestones = gitlab.get_milestones()
    milestone_map = {}  # gitlab milestone id -> github milestone number
    for m in gl_milestones:
        gh_number = github.ensure_milestone(
            title=m["title"],
            description=m.get("description"),
            due_on=m.get("due_date"),
            state=m.get("state"),
        )
        milestone_map[m["id"]] = gh_number
    log.info("Milestones synced: %d", len(milestone_map))

    # --- Migrate issues ---
    migrated = 0
    skipped = 0
    errors = 0

    for issue in gitlab.get_issues():
        iid = issue["iid"]
        title = issue["title"]

        if state.is_migrated(iid):
            log.debug("Skipping already-migrated issue #%d: %s", iid, title)
            skipped += 1
            continue

        try:
            log.info("Migrating issue #%d: %s", iid, title)

            # Ensure labels
            label_names = [lbl for lbl in issue.get("labels", [])]
            for lbl in label_names:
                github.ensure_label(lbl)

            # Build body
            body = format_issue_body(issue, project_path)

            # Resolve milestone
            milestone_number = None
            if issue.get("milestone"):
                milestone_number = milestone_map.get(issue["milestone"]["id"])

            # Resolve assignees
            assignees = []
            for a in issue.get("assignees", []):
                gl_user = a.get("username", "")
                gh_user = USERNAME_MAP.get(gl_user, gl_user)
                assignees.append(gh_user)

            # Create issue
            gh_issue_number = github.create_issue(
                title=title,
                body=body,
                labels=label_names,
                milestone_number=milestone_number,
                assignees=assignees,
            )

            # Migrate comments
            notes = gitlab.get_issue_notes(iid)
            for note in notes:
                comment_body = format_comment(note, project_path)
                github.add_comment(gh_issue_number, comment_body)

            if notes:
                log.info("  Added %d comments", len(notes))

            # Close issue if needed
            if issue.get("state") == "closed":
                github.close_issue(gh_issue_number)

            state.record(iid, gh_issue_number)
            migrated += 1
            log.info("  -> GitHub issue #%s created", gh_issue_number)

        except Exception:
            log.exception("Failed to migrate issue #%d: %s", iid, title)
            errors += 1

    log.info("=== Migration complete ===")
    log.info("Migrated: %d | Skipped: %d | Errors: %d", migrated, skipped, errors)


if __name__ == "__main__":
    migrate()
