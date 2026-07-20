"""Generate an AI Site Factory administrator password hash without storing the password."""

from getpass import getpass
import base64
import hashlib
import os


def main() -> None:
    password = getpass("New administrator password (12+ characters): ")
    confirmation = getpass("Confirm administrator password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters.")

    iterations = 600_000
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_text = base64.urlsafe_b64encode(derived).decode("ascii").rstrip("=")
    print(f"pbkdf2_sha256${iterations}${salt_text}${digest_text}")


if __name__ == "__main__":
    main()
