"""Release signing: Ed25519. Private key stays OFF the web server (README 12).

sign <bundle> -> .sig ; verify <bundle> <sig> with public key.
Pi verifies before A/B install; rollback on health-check fail (see pi/nsl_pi/update.py).
"""
from __future__ import annotations

import sys


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def sign_file(bundle: str, privkey_pem: str, sig_out: str):
    from cryptography.hazmat.primitives import serialization
    priv = serialization.load_pem_private_key(open(privkey_pem, "rb").read(), password=None)
    sig = priv.sign(open(bundle, "rb").read())
    open(sig_out, "wb").write(sig)
    print(f"signed {bundle} -> {sig_out} ({len(sig)} bytes)")


def verify_file(bundle: str, sig: str, pubkey_pem: str) -> bool:
    from cryptography.hazmat.primitives import serialization
    pub = serialization.load_pem_public_key(open(pubkey_pem, "rb").read())
    try:
        pub.verify(open(sig, "rb").read(), open(bundle, "rb").read())
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: release.py genkey|sign|verify ...")
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "genkey":
        from cryptography.hazmat.primitives import serialization
        priv, pub = _keypair()
        open("release-priv.pem", "wb").write(priv.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        open("release-pub.pem", "wb").write(pub.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        print("wrote release-priv.pem (KEEP OFF SERVER) + release-pub.pem (bake into Pi image)")
    elif cmd == "sign":
        sign_file(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "verify":
        ok = verify_file(sys.argv[2], sys.argv[3], sys.argv[4])
        print("OK" if ok else "BAD")
        sys.exit(0 if ok else 1)
