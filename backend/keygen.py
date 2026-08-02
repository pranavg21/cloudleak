"""Mint an API key and print the env line to deploy with.

Usage: python keygen.py [label]

Only the SHA-256 digest is stored server-side, so the plaintext shown here is
the only copy. Save it now; it cannot be recovered from the digest.
"""

from __future__ import annotations

import sys

from core.config import generate_dev_key


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "default"
    raw, digest = generate_dev_key()
    print(f"\nAPI key ({label}) -- shown once, store it in your secret manager:\n")
    print(f"  {raw}\n")
    print("Deploy the server with:\n")
    print(f"  CLOUDLEAK_API_KEY_HASHES={digest}\n")
    print("Add more keys by comma-separating their digests.\n")


if __name__ == "__main__":
    main()
