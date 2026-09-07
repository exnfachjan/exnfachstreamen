"""Keeps api_key and streams.db in sync (fork of streamserver's apikey.py).

sls creates an admin API key on first start and logs it exactly once.
Scraping that log line is fragile (depends on log level/wording) and breaks
silently once the database and the key file drift apart (restored backup,
deleted database, half-copied volume). Instead: check the stored key against
the database (it only keeps the SHA-256) and mint + insert a new one if it
doesn't match. Existing stream-id entries are untouched either way.
"""
import hashlib
import os
import secrets
import sqlite3
import string
import sys

DB = sys.argv[1]
KEYFILE = sys.argv[2]
KEY_NAME = "ingest-entrypoint"
ALPHABET = string.ascii_letters + string.digits


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def read_keyfile():
    try:
        with open(KEYFILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_keyfile(key):
    with open(KEYFILE, "w") as fh:
        fh.write(key + "\n")
    os.chmod(KEYFILE, 0o600)


def main():
    if not os.path.exists(DB):
        print("[apikey] streams.db missing - skipped.")
        return 1

    con = sqlite3.connect(DB)
    try:
        hashes = {
            row[0]
            for row in con.execute(
                "SELECT key_hash FROM api_keys WHERE active = 1 "
                "AND permissions LIKE '%admin%'"
            )
        }

        key = read_keyfile()
        if key and sha256(key) in hashes:
            print("[apikey] stored key matches the database.")
            return 0

        if key:
            print("[apikey] stored key does NOT match the database - minting a new one.")
        else:
            print("[apikey] no key stored - minting one.")

        key = "".join(secrets.choice(ALPHABET) for _ in range(32))
        con.execute(
            "INSERT INTO api_keys (key_hash, name, permissions, active) VALUES (?, ?, 'admin', 1)",
            (sha256(key), KEY_NAME),
        )
        con.commit()
        write_keyfile(key)
        print("[apikey] new admin key inserted and saved.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
