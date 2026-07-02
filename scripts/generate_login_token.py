#!/usr/bin/env python3
"""
Run this ONCE on your own computer to log into Garmin Connect and produce a
token bundle you'll paste into a GitHub Actions secret.

Why: Garmin's login can require an MFA code, which only works interactively.
This script does that interactive login one time, saves the resulting
OAuth tokens, and packages them into a single base64 string. GitHub Actions
will reuse that token bundle on every scheduled run instead of logging in
with your password each time.

Usage:
    pip install garminconnect
    python3 scripts/generate_login_token.py
"""
import base64
import getpass
import io
import tarfile
from pathlib import Path

from garminconnect import Garmin

TOKEN_DIR = Path.home() / ".garminconnect"


def main() -> None:
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    client = Garmin(
        email,
        password,
        prompt_mfa=lambda: input("Enter the MFA code Garmin just sent you: ").strip(),
    )
    client.login(str(TOKEN_DIR))
    print(f"\nLogin succeeded. Tokens saved to {TOKEN_DIR}")

    # Package the token directory into a single base64 string.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(TOKEN_DIR, arcname="garminconnect")
    encoded = base64.b64encode(buf.getvalue()).decode()

    out_file = Path("garmin_tokens_b64.txt")
    out_file.write_text(encoded)
    print(f"\nWrote base64 token bundle to {out_file.resolve()}")
    print(
        "\nNext step:\n"
        "  1. Open your GitHub repo → Settings → Secrets and variables → Actions\n"
        "  2. Create a secret named GARMIN_TOKENS_B64\n"
        f"  3. Paste the entire contents of {out_file.name} as the value\n"
        "  4. Delete the local file afterwards (it contains your session tokens):\n"
        f"     rm {out_file.name}\n"
    )


if __name__ == "__main__":
    main()
