#!/usr/bin/env python3
"""Migrate GitLab issues to GitHub, preserving labels, milestones, comments, and state."""

import base64
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
try:
    USERNAME_MAP = json.loads(os.getenv("USERNAME_MAP", "{}"))
except json.JSONDecodeError:
    log.error("USERNAME_MAP environment variable contains invalid JSON.")
    sys.exit(1)
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
MIGRATE_IMAGES = os.getenv("MIGRATE_IMAGES", "1") == "1"
STATE_FILE = os.getenv("MIGRATION_STATE_FILE", ".migration_state.json")

# Default colors for GitLab scoped labels (scope prefix -> hex color)
# Used as fallback when GitLab label has no color or for consistent grouping
SCOPE_COLORS = {
    "x-priority": "d93f0b",   # red
    "x-type": "0075ca",       # blue
    "x-team": "0e8a16",       # green
    "x-workflow": "5319e7",    # purple
    "status": "fbca04",        # yellow
}
try:
    SCOPE_COLORS.update(json.loads(os.getenv("SCOPE_COLORS", "{}")))
except json.JSONDecodeError:
    log.warning("SCOPE_COLORS contains invalid JSON, using defaults.")


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
        return list(self._get_paginated(endpoint, {"sort": "asc", "order_by": "created_at"}))

    def get_milestones(self):
        endpoint = f"/projects/{self.project_id}/milestones"
        return list(self._get_paginated(endpoint, {"state": "all"}))

    def get_labels(self):
        endpoint = f"/projects/{self.project_id}/labels"
        return list(self._get_paginated(endpoint))

    def get_issue_award_emoji(self, iid):
        endpoint = f"/projects/{self.project_id}/issues/{iid}/award_emoji"
        return list(self._get_paginated(endpoint))

    def get_note_award_emoji(self, iid, note_id):
        endpoint = f"/projects/{self.project_id}/issues/{iid}/notes/{note_id}/award_emoji"
        return list(self._get_paginated(endpoint))

    def get_issue_links(self, iid):
        endpoint = f"/projects/{self.project_id}/issues/{iid}/links"
        return list(self._get_paginated(endpoint))

    def get_related_merge_requests(self, iid):
        endpoint = f"/projects/{self.project_id}/issues/{iid}/related_merge_requests"
        return list(self._get_paginated(endpoint))

    def download_upload(self, project_path, upload_path):
        """Download a file from GitLab uploads. Returns (bytes, content_type) or (None, None)."""
        url = f"{self.base_url.rsplit('/api/v4', 1)[0]}/{project_path}{upload_path}"
        resp = self.session.get(url, stream=True)
        if resp.status_code == 200:
            return resp.content, resp.headers.get("Content-Type", "application/octet-stream")
        log.warning("  Failed to download %s: %s", upload_path, resp.status_code)
        return None, None

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
        self._milestones_fetched = False

    def _request(self, method, endpoint, **kwargs):
        allow_404 = kwargs.pop("allow_404", False)
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
            # Return response without raising for expected non-error codes
            if allow_404 and resp.status_code == 404:
                return resp
            resp.raise_for_status()
            # Throttle writes to avoid abuse detection
            if method.upper() in ("POST", "PATCH", "PUT"):
                time.sleep(1)
            return resp

    def ensure_label(self, name, color=None, description=None):
        if name in self._label_cache:
            return
        endpoint = f"/repos/{self.owner}/{self.repo}/labels/{requests.utils.quote(name, safe='')}"
        check = self._request("GET", endpoint, allow_404=True)
        if check.status_code == 200:
            self._label_cache.add(name)
            return
        if DRY_RUN:
            log.info("[DRY RUN] Would create label: %s (color=%s)", name, color)
            self._label_cache.add(name)
            return
        gh_color = (color or "").lstrip("#")
        # Use scope-based color for scoped labels (e.g. "x-priority :: P1")
        if not gh_color or gh_color == "ededed":
            scope = name.split("::")[0].strip() if "::" in name else ""
            gh_color = SCOPE_COLORS.get(scope, "ededed")
        payload = {"name": name, "color": gh_color}
        if description:
            payload["description"] = description[:100]  # GitHub limits to 100 chars
        self._request("POST", f"/repos/{self.owner}/{self.repo}/labels", json=payload)
        self._label_cache.add(name)
        log.info("Created label: %s (color=#%s)", name, gh_color)

    def ensure_milestone(self, title, description=None, due_on=None, state=None):
        if title in self._milestone_cache:
            return self._milestone_cache[title]
        # Fetch existing milestones (once)
        if not self._milestones_fetched:
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
            self._milestones_fetched = True
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
            return None
        resp = self._request("POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                       json={"body": body})
        return resp.json()["id"]

    def add_reaction(self, issue_number, reaction, comment_id=None):
        if DRY_RUN:
            log.info("[DRY RUN] Would add reaction %s to issue #%d", reaction, issue_number)
            return
        if comment_id:
            endpoint = f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}/reactions"
        else:
            endpoint = f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/reactions"
        # Temporarily override Accept header for reactions API
        original_accept = self.session.headers.get("Accept")
        self.session.headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"
        try:
            self._request("POST", endpoint, json={"content": reaction})
        except requests.HTTPError as e:
            # 422 means reaction already exists or is invalid — not fatal
            if e.response is not None and e.response.status_code == 422:
                pass
            else:
                log.warning("Failed to add reaction %s: %s", reaction, e)
        finally:
            self.session.headers["Accept"] = original_accept

    def upload_file(self, repo_path, content_bytes, commit_message="Upload migrated asset"):
        """Upload a file to the repo via the Contents API. Returns the raw download URL."""
        if DRY_RUN:
            log.info("[DRY RUN] Would upload file: %s", repo_path)
            return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/main/{repo_path}"
        encoded = base64.b64encode(content_bytes).decode("ascii")
        # Check if file already exists (idempotent)
        endpoint = f"/repos/{self.owner}/{self.repo}/contents/{repo_path}"
        check = self._request("GET", endpoint, allow_404=True)
        if check.status_code == 200:
            return check.json().get("download_url", "")
        resp = self._request("PUT", endpoint, json={
            "message": commit_message,
            "content": encoded,
        })
        return resp.json().get("content", {}).get("download_url", "")

    def close_issue(self, issue_number):
        if DRY_RUN:
            log.info("[DRY RUN] Would close issue #%d", issue_number)
            return
        self._request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                       json={"state": "closed"})


# ---------------------------------------------------------------------------
# Content transformation
# ---------------------------------------------------------------------------


# GitLab emoji name -> GitHub reaction content
# GitHub only supports: +1, -1, laugh, confused, heart, hooray, rocket, eyes
GITLAB_TO_GITHUB_EMOJI = {
    "thumbsup": "+1",
    "thumbsdown": "-1",
    "laughing": "laugh",
    "smiley": "laugh",
    "confused": "confused",
    "heart": "heart",
    "tada": "hooray",
    "rocket": "rocket",
    "eyes": "eyes",
    "100": "+1",
    "clap": "hooray",
    "fire": "hooray",
    "star": "hooray",
    "thinking": "confused",
}


def map_username(match):
    gl_user = match.group(1)
    gh_user = USERNAME_MAP.get(gl_user)
    if gh_user:
        return f"@{gh_user}"
    return f"`@{gl_user}`"


# Cache for uploaded images: gitlab upload path -> github raw URL
_image_cache = {}


def migrate_uploads_in_text(body, gitlab_project_path, gitlab_client, github_client):
    """Find GitLab upload references, download and re-upload to GitHub, rewrite URLs."""
    if not body or not gitlab_project_path:
        return body

    upload_pattern = re.compile(r"(/uploads/([a-f0-9]{32})/([^\s)]+))")

    def replace_upload(match):
        upload_path = match.group(1)  # /uploads/hash/filename
        upload_hash = match.group(2)
        filename = match.group(3)

        # Check cache first
        if upload_path in _image_cache:
            return _image_cache[upload_path]

        # Download from GitLab
        content, _ = gitlab_client.download_upload(gitlab_project_path, upload_path)
        if content is None:
            # Fallback: use absolute GitLab URL
            fallback = f"{GITLAB_URL}/{gitlab_project_path}{upload_path}"
            _image_cache[upload_path] = fallback
            return fallback

        # Upload to GitHub repo
        repo_path = f".github/migration-assets/{upload_hash}/{filename}"
        github_url = github_client.upload_file(
            repo_path, content,
            commit_message=f"Upload migrated asset: {filename}",
        )
        if github_url:
            _image_cache[upload_path] = github_url
            log.info("    Migrated upload: %s -> %s", filename, repo_path)
            return github_url

        # Fallback if upload failed
        fallback = f"{GITLAB_URL}/{gitlab_project_path}{upload_path}"
        _image_cache[upload_path] = fallback
        return fallback

    return upload_pattern.sub(replace_upload, body)


def convert_body(body, gitlab_project_path="", gitlab_client=None, github_client=None):
    if not body:
        return ""
    # Map @username references (negative lookbehind avoids matching emails like user@domain)
    body = re.sub(r"(?<!\w)@(\w+)", map_username, body)
    # Migrate uploads: download from GitLab, upload to GitHub, rewrite URLs
    if MIGRATE_IMAGES and gitlab_client and github_client:
        body = migrate_uploads_in_text(body, gitlab_project_path, gitlab_client, github_client)
    elif gitlab_project_path:
        # Fallback: just convert to absolute GitLab URLs
        body = re.sub(
            r"(/uploads/[a-f0-9]{32}/[^\s)]+)",
            rf"{GITLAB_URL}/{gitlab_project_path}\1",
            body,
        )
    return body


def format_issue_body(issue, gitlab_project_path="", linked_issues=None,
                      related_mrs=None, gitlab_client=None, github_client=None):
    author = issue.get("author", {})
    author_name = author.get("name", "Unknown")
    author_username = author.get("username", "unknown")
    created_at = issue.get("created_at", "")[:10]

    header = (
        f"> *Migrated from GitLab issue #{issue['iid']}, "
        f"originally created by **{author_name}** (`@{author_username}`) on {created_at}*\n\n"
    )
    body = convert_body(issue.get("description", ""), gitlab_project_path,
                        gitlab_client, github_client)

    # --- Metadata table ---
    metadata_rows = []
    if issue.get("due_date"):
        metadata_rows.append(f"| Due date | {issue['due_date']} |")
    if issue.get("weight"):
        metadata_rows.append(f"| Weight | {issue['weight']} |")
    time_stats = issue.get("time_stats", {})
    if time_stats.get("human_time_estimate"):
        metadata_rows.append(f"| Time estimate | {time_stats['human_time_estimate']} |")
    if time_stats.get("human_total_time_spent"):
        metadata_rows.append(f"| Time spent | {time_stats['human_total_time_spent']} |")
    if issue.get("confidential"):
        metadata_rows.append("| Confidential | Yes |")
    if issue.get("state") == "closed":
        closed_by = issue.get("closed_by", {})
        closed_at = issue.get("closed_at", "")[:10] if issue.get("closed_at") else ""
        closed_name = closed_by.get("name", "Unknown") if closed_by else "Unknown"
        closed_username = closed_by.get("username", "") if closed_by else ""
        closer = f"**{closed_name}** (`@{closed_username}`)" if closed_username else closed_name
        metadata_rows.append(f"| Closed by | {closer} on {closed_at} |")
    if issue.get("updated_at"):
        metadata_rows.append(f"| Last updated | {issue['updated_at'][:10]} |")

    if metadata_rows:
        body += "\n\n---\n\n### GitLab Metadata\n\n| Field | Value |\n|---|---|\n"
        body += "\n".join(metadata_rows)

    # --- Linked issues ---
    if linked_issues:
        body += "\n\n### Linked Issues\n\n"
        for link in linked_issues:
            link_type = link.get("link_type", "relates_to")
            ref = link.get("references", {}).get("full", f"#{link.get('iid', '?')}")
            link_title = link.get("title", "")
            body += f"- **{link_type}**: {ref} — {link_title}\n"

    # --- Related merge requests ---
    if related_mrs:
        body += "\n\n### Related Merge Requests\n\n"
        for mr in related_mrs:
            ref = mr.get("references", {}).get("full", f"!{mr.get('iid', '?')}")
            mr_title = mr.get("title", "")
            mr_state = mr.get("state", "")
            mr_url = mr.get("web_url", "")
            if mr_url:
                body += f"- [{ref}]({mr_url}) ({mr_state}) — {mr_title}\n"
            else:
                body += f"- {ref} ({mr_state}) — {mr_title}\n"

    return header + body


def format_comment(note, gitlab_project_path="", gitlab_client=None, github_client=None):
    author = note.get("author", {})
    author_name = author.get("name", "Unknown")
    author_username = author.get("username", "unknown")
    created_at = note.get("created_at", "")[:10]
    is_system = note.get("system", False)

    if is_system:
        header = f"> *System event by **{author_name}** on {created_at}:*\n\n"
    else:
        header = f"> **{author_name}** (`@{author_username}`) commented on {created_at}:\n\n"
    body = convert_body(note.get("body", ""), gitlab_project_path,
                        gitlab_client, github_client)
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

    # --- Pre-fetch GitLab labels (for colors and descriptions) ---
    log.info("Fetching GitLab labels...")
    gl_labels = gitlab.get_labels()
    label_details = {}  # name -> {color, description}
    for lbl in gl_labels:
        label_details[lbl["name"]] = {
            "color": lbl.get("color"),
            "description": lbl.get("description"),
        }
    log.info("Found %d GitLab labels", len(label_details))

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

            # Ensure labels (with original colors and descriptions from GitLab)
            label_names = [lbl for lbl in issue.get("labels", [])]
            for lbl in label_names:
                details = label_details.get(lbl, {})
                github.ensure_label(lbl, color=details.get("color"),
                                    description=details.get("description"))

            # Fetch linked issues and related merge requests
            linked_issues = []
            related_mrs = []
            try:
                linked_issues = gitlab.get_issue_links(iid)
            except Exception:
                log.warning("  Could not fetch linked issues for #%d", iid)
            try:
                related_mrs = gitlab.get_related_merge_requests(iid)
            except Exception:
                log.warning("  Could not fetch related MRs for #%d", iid)

            # Build body
            body = format_issue_body(issue, project_path,
                                     linked_issues=linked_issues,
                                     related_mrs=related_mrs,
                                     gitlab_client=gitlab,
                                     github_client=github)

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

            # Migrate issue-level reactions
            try:
                issue_emoji = gitlab.get_issue_award_emoji(iid)
                for emoji in issue_emoji:
                    gh_reaction = GITLAB_TO_GITHUB_EMOJI.get(emoji.get("name"))
                    if gh_reaction:
                        github.add_reaction(gh_issue_number, gh_reaction)
                if issue_emoji:
                    log.info("  Added %d reactions to issue", len(issue_emoji))
            except Exception:
                log.warning("  Could not fetch reactions for issue #%d", iid)

            # Migrate comments and their reactions
            notes = gitlab.get_issue_notes(iid)
            for note in notes:
                comment_body = format_comment(note, project_path,
                                              gitlab_client=gitlab,
                                              github_client=github)
                gh_comment_id = github.add_comment(gh_issue_number, comment_body)

                # Migrate comment-level reactions
                if gh_comment_id:
                    try:
                        note_emoji = gitlab.get_note_award_emoji(iid, note["id"])
                        for emoji in note_emoji:
                            gh_reaction = GITLAB_TO_GITHUB_EMOJI.get(emoji.get("name"))
                            if gh_reaction:
                                github.add_reaction(gh_issue_number, gh_reaction,
                                                    comment_id=gh_comment_id)
                    except Exception:
                        log.warning("  Could not migrate reactions for note %d", note["id"])

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
