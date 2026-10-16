"""Client-side helper: re-run Google's OAuth consent to refresh a test token.json.

This stands in for what a real client agent does; the server never runs it.
It reuses the client ID/secret from the existing token file, opens a browser
for consent, and rewrites the file (keeping a .bak of the old one).

    uv run --with google-auth-oauthlib python scripts/reauthorize.py [--token-file PATH]
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "gtasks-mcp" / "token.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    args = parser.parse_args()

    old = json.loads(args.token_file.read_text())
    client_config = {
        "installed": {
            "client_id": old["client_id"],
            "client_secret": old["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": old.get("token_uri", "https://oauth2.googleapis.com/token"),
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=[TASKS_SCOPE])
    credentials = flow.run_local_server(port=0, prompt="consent", timeout_seconds=600)

    shutil.copy2(args.token_file, args.token_file.with_suffix(".json.bak"))
    args.token_file.write_text(credentials.to_json())
    args.token_file.chmod(0o600)
    print(f"Wrote fresh credentials to {args.token_file}")


if __name__ == "__main__":
    main()
