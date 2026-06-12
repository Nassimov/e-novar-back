#!/usr/bin/env python3
"""
Generate admin credentials for Railway environment variables.

Run locally (NOT in production):
  python3 scripts/generate_admin_credentials.py

Then set the output values in Railway → Service → Variables.
NEVER commit these values to git.
"""

import getpass
import secrets
import sys


def main():
    try:
        import bcrypt
        import pyotp
    except ImportError:
        print("Install dependencies first:")
        print("  pip install bcrypt pyotp")
        sys.exit(1)

    print("=" * 60)
    print("E-NOVAR Admin Credentials Generator")
    print("=" * 60)
    print()

    email = input("Admin email: ").strip()
    if not email:
        print("Email required.")
        sys.exit(1)

    password = getpass.getpass("Admin password (hidden): ")
    if len(password) < 12:
        print("Password must be at least 12 characters.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm password (hidden): ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)

    # Hash with bcrypt directly (avoids passlib/bcrypt 4.x compatibility bug)
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    password_hash = bcrypt.hashpw(password_bytes, salt).decode("utf-8")

    totp_secret = pyotp.random_base32()
    totp = pyotp.TOTP(totp_secret)
    jwt_secret = secrets.token_hex(32)

    print()
    print("=" * 60)
    print("Set these in Railway → Service → Variables:")
    print("=" * 60)
    print()
    print(f"ADMIN_EMAIL={email}")
    print(f"ADMIN_PASSWORD_HASH={password_hash}")
    print(f"ADMIN_2FA_SECRET={totp_secret}")
    print(f"ADMIN_JWT_EXPIRE_MINUTES=60")
    print(f"ADMIN_JWT_SECRET={jwt_secret}")
    print()
    print("=" * 60)
    print("Scan this URI in your Authenticator app (Google / Microsoft / Authy):")
    print("=" * 60)
    print()
    uri = totp.provisioning_uri(email, issuer_name="E-NOVAR Admin")
    print(f"Secret : {totp_secret}")
    print(f"URI    : {uri}")
    print()

    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(Astuce: pip install qrcode  pour afficher le QR code dans le terminal)")
        print()

    print("Vérification — entrez le code affiché par votre Authenticator :")
    test_code = input("Code 2FA (6 chiffres) : ").strip()
    if totp.verify(test_code, valid_window=1):
        print("TOTP : OK ✓")
    else:
        print("TOTP : ÉCHEC — relancez le script et re-scannez le QR code.")
        sys.exit(1)

    print()
    print("Credentials générés avec succès.")
    print("IMPORTANT : ne sauvegardez ces valeurs NULLE PART sauf dans Railway Variables.")


if __name__ == "__main__":
    main()
