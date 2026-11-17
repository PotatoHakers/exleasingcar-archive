"""Safely compare the credentials saved in keyring with manually entered ones.

The script prints only match results and the keyring backend name. It never
prints the login or password, and it does not modify the credential store.
Run it directly as a .py file from the activated project virtual environment.
"""

from __future__ import annotations

from getpass import getpass

import keyring

SERVICE = "exleasingcar-archive"
LOGIN_KEY = "login"
PASSWORD_KEY = "password"


def main() -> int:
    try:
        stored_login = keyring.get_password(SERVICE, LOGIN_KEY)
        stored_password = keyring.get_password(SERVICE, PASSWORD_KEY)
        backend_name = type(keyring.get_keyring()).__name__
    except Exception as exc:  # Show only the error type, not backend internals.
        print(f"Keyring read failed: {type(exc).__name__}")
        return 1

    print(f"Keyring backend: {backend_name}")
    print(f"Stored login present: {bool(stored_login)}")
    print(f"Stored password present: {bool(stored_password)}")

    entered_login = input("Enter the login you expect the collector to use: ").strip()
    entered_password = getpass("Enter the password you expect the collector to use: ")

    login_matches = bool(stored_login) and stored_login.strip() == entered_login
    password_matches = bool(stored_password) and stored_password == entered_password

    print(f"Login matches: {login_matches}")
    print(f"Password matches: {password_matches}")
    return 0 if login_matches and password_matches else 2


if __name__ == "__main__":
    raise SystemExit(main())
