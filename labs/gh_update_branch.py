#!/usr/bin/env python3
"""Update files on an EXISTING branch via the GitHub API (custom.github credential).
Mirrors github_api.py push-main, but PATCHes refs/heads/<branch> instead of main.
Usage: gh_update_branch.py --owner O --repo R --branch B --message M --local-dir D --files F...
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/workspace/custom-connectors/custom.github/bin"))
import github_api


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--message", required=True)
    ap.add_argument("--local-dir", required=True)
    ap.add_argument("--files", nargs="+", required=True)
    a = ap.parse_args()
    base = f"https://api.github.com/repos/{a.owner}/{a.repo}"
    status, _, _, ref = github_api.call("GET", f"{base}/git/ref/heads/{a.branch}")
    if status != 200:
        print(json.dumps({"status": status, "step": "get_branch_ref", "detail": ref},
                         ensure_ascii=False))
        return 1
    base_sha = ref["object"]["sha"]
    status, _, _, commit = github_api.call("GET", f"{base}/git/commits/{base_sha}")
    if status != 200:
        print(json.dumps({"status": status, "step": "get_branch_commit", "detail": commit},
                         ensure_ascii=False))
        return 1
    base_tree = commit["tree"]["sha"]
    entries = []
    for rel in a.files:
        with open(os.path.join(a.local_dir, rel), "rb") as f:
            content = base64.b64encode(f.read()).decode()
        status, _, _, blob = github_api.call(
            "POST", f"{base}/git/blobs", {"content": content, "encoding": "base64"})
        if status not in (200, 201):
            print(json.dumps({"status": status, "step": "create_blob", "file": rel,
                              "detail": blob}, ensure_ascii=False))
            return 1
        entries.append({"path": rel, "mode": "100644", "type": "blob",
                        "sha": blob["sha"]})
    status, _, _, tree = github_api.call(
        "POST", f"{base}/git/trees", {"base_tree": base_tree, "tree": entries})
    if status not in (200, 201):
        print(json.dumps({"status": status, "step": "create_tree", "detail": tree},
                         ensure_ascii=False))
        return 1
    status, _, _, new_commit = github_api.call(
        "POST", f"{base}/git/commits",
        {"message": a.message, "tree": tree["sha"], "parents": [base_sha]})
    if status not in (200, 201):
        print(json.dumps({"status": status, "step": "create_commit", "detail": new_commit},
                         ensure_ascii=False))
        return 1
    status, _, _, upd = github_api.call(
        "PATCH", f"{base}/git/refs/heads/{a.branch}",
        {"sha": new_commit["sha"], "force": False})
    if status not in (200, 201):
        print(json.dumps({"status": status, "step": "patch_branch_ref", "detail": upd},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"status": status, "base_sha": base_sha,
                      "new_commit": new_commit["sha"], "ref": upd.get("ref")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
