"""Ręczna lista loginów odbiorców powiadomień."""

from __future__ import annotations

import os


class UsersError(Exception):
    pass


def load_users(path: str = "users.txt") -> set[str]:
    if not os.path.isfile(path):
        raise UsersError(f"Brak pliku użytkowników: {path}")

    with open(path, encoding="utf-8") as file:
        users = {
            line.strip()
            for line in file
            if line.strip() and not line.lstrip().startswith("#")
        }

    if not users:
        raise UsersError(f"Plik użytkowników {path} nie zawiera żadnych loginów")
    return users
