from getpass import getpass
import sys

from app import reset_main_admin_password


def main():
    email = input("Main administrator email: ")
    password = getpass("New password (at least 12 characters): ")
    confirmation = getpass("Confirm new password: ")
    if password != confirmation:
        print("Error: passwords do not match.", file=sys.stderr)
        return 1

    try:
        reset_main_admin_password(email, password)
    except (LookupError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print("Main administrator password reset successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
