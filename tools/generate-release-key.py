#!/usr/bin/env python3
"""Generate an Ed25519 key pair for signing OTA release bundles.

Usage:
  python3 tools/generate-release-key.py

Prints the private key (PEM) to stdout and writes the public key to
./release.pub. Keep the private key secret (set as NSL_RELEASE_PRIVATE_KEY
on the portal host). Install release.pub on each Pi via:

  --release-key /path/to/release.pub

or by setting NSL_RELEASE_PUBLIC_KEY when running pi/install.sh.
"""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, PublicFormat, NoEncryption)


def main():
    key = Ed25519PrivateKey.generate()
    priv_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    pub_pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    with open("release.pub", "w") as f:
        f.write(pub_pem)
    print("# Private key - paste this into NSL_RELEASE_PRIVATE_KEY on the portal host")
    print("# Public key written to release.pub")
    print(priv_pem)


if __name__ == "__main__":
    main()
