#!/usr/bin/env python3
"""Read a file from a GitHub repo branch (decoded) via the custom.github credential.
Usage: gh_read_file.py --owner O --repo R --path P --ref REF  -> prints file content.
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/workspace/custom-connectors/custom.github/bin"))
import github_api

ap = argparse.ArgumentParser()
ap.add_argument("--owner", required=True)
ap.add_argument("--repo", required=True)
ap.add_argument("--path", required=True)
ap.add_argument("--ref", required=True)
a = ap.parse_args()
url = (f"https://api.github.com/repos/{a.owner}/{a.repo}/contents/{a.path}"
       f"?ref={a.ref}")
status, _, _, out = github_api.call("GET", url)
if status != 200:
    print(json.dumps({"status": status, "error": out}, ensure_ascii=False),
          file=sys.stderr)
    sys.exit(1)
sys.stdout.write(base64.b64decode(out["content"]).decode("utf-8"))
