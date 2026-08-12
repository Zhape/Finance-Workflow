"""A minimal GitHub client: branch, commit files, open a pull request.

Only what the workflow-request pipeline needs, on purpose. The token behind
this can write to the repository, which makes it the most powerful secret on
the worker after the database URL -- so the surface area that touches it is
kept small enough to audit in one sitting, rather than pulling in a full
GitHub SDK for four calls.
"""

from __future__ import annotations

import base64
import os

import requests

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("FW_GITHUB_TOKEN", "").strip()
                and os.environ.get("FW_GITHUB_REPO", "").strip())


class GitHubClient:
    def __init__(self):
        self._token = os.environ.get("FW_GITHUB_TOKEN", "").strip()
        self.repo = os.environ.get("FW_GITHUB_REPO", "").strip()
        if not self._token or not self.repo:
            raise GitHubError(
                "GitHub is not configured. Set FW_GITHUB_TOKEN (a fine-grained "
                "token with contents and pull-request write on the repo) and "
                "FW_GITHUB_REPO (owner/name) on the worker."
            )

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = requests.request(
            method, f"{API}{path}", json=payload, timeout=60,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "finance-workflows-worker",
            },
        )
        if resp.status_code >= 400:
            # The response body names the problem (bad ref, missing permission)
            # but can also echo request content, so keep only the message.
            try:
                message = resp.json().get("message", resp.text[:200])
            except ValueError:
                message = resp.text[:200]
            raise GitHubError(f"GitHub {method} {path} failed "
                              f"({resp.status_code}): {message}")
        return resp.json() if resp.text else {}

    def default_branch(self) -> str:
        return self._call("GET", f"/repos/{self.repo}")["default_branch"]

    def create_branch(self, name: str, from_branch: str) -> None:
        sha = self._call(
            "GET", f"/repos/{self.repo}/git/ref/heads/{from_branch}"
        )["object"]["sha"]
        self._call("POST", f"/repos/{self.repo}/git/refs",
                   {"ref": f"refs/heads/{name}", "sha": sha})

    def get_file(self, path: str, ref: str) -> tuple[str, str]:
        """Return (text, blob_sha) for an existing file."""
        data = self._call("GET", f"/repos/{self.repo}/contents/{path}?ref={ref}")
        return (base64.b64decode(data["content"]).decode("utf-8"), data["sha"])

    def put_file(self, path: str, text: str, message: str, branch: str,
                 sha: str | None = None) -> None:
        payload = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        self._call("PUT", f"/repos/{self.repo}/contents/{path}", payload)

    def open_pr(self, title: str, head: str, base: str, body: str) -> str:
        pr = self._call("POST", f"/repos/{self.repo}/pulls",
                        {"title": title, "head": head, "base": base, "body": body})
        return pr["html_url"]
