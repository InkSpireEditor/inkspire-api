# -*- coding: utf-8 -*-
"""Account administration from a shell.

The API exposes no registration and no password change, so these two commands are
the only way to create an account or set its password. Both act without knowing the
current password, which is why they have no HTTP equivalent.

    inkspire user create alice@example.com
    inkspire user create alice@example.com --password '...' --role ROLE_ADMIN
    inkspire user reset-password alice@example.com
    inkspire user reset-password alice@example.com --keep-sessions
"""

from __future__ import annotations

import re
import secrets
from typing import Annotated

import typer
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .db import get_sessionmaker
from .models import MAX_EMAIL_LENGTH, RefreshToken, User
from .security import hash_password
from .settings import get_settings

#: Bytes of randomness behind a generated password. Hex-encoded, so 24 characters.
GENERATED_PASSWORD_BYTES = 12

MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 4096

#: A local part, an @, and a domain containing a dot. This catches a mistyped
#: address; it is not an RFC 5322 check, and only delivery proves an address real.
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = typer.Typer(help="InkSpire administration.", no_args_is_help=True)
user_app = typer.Typer(help="Account administration.", no_args_is_help=True)
app.add_typer(user_app, name="user")

EmailArgument = Annotated[str, typer.Argument(help="Email address of the account")]
PasswordOption = Annotated[
    str | None,
    typer.Option(
        "--password", "-p", help="Omit to generate a random one and print it."
    ),
]


def fail(message: str) -> None:
    """Prints an error to stderr and exits non-zero."""
    typer.secho(f"[ERROR] {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def generate_password() -> str:
    return secrets.token_hex(GENERATED_PASSWORD_BYTES)


def check_password(password: str) -> None:
    """Rejects a password outside the accepted length range.

    The upper bound is a policy limit. Only the first 72 bytes of a password reach
    bcrypt, whatever length is allowed here.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        fail(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        fail(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")


def normalise_roles(roles: list[str]) -> list[str]:
    checked = []
    for role in roles:
        role = role.strip().upper()
        if not role.startswith("ROLE_"):
            fail(f'Invalid role "{role}": roles must start with ROLE_.')
        checked.append(role)
    return list(dict.fromkeys(checked))


@user_app.command("create")
def create(
    email: EmailArgument,
    password: PasswordOption = None,
    role: Annotated[
        list[str] | None,
        typer.Option("--role", "-r", help="Grant a role (repeatable). ROLE_USER is always implied."),
    ] = None,
) -> None:
    """Create an account."""
    settings = get_settings()
    email = email.strip()

    if not EMAIL.match(email):
        fail(f'"{email}" is not a valid email address.')
    if len(email) > MAX_EMAIL_LENGTH:
        fail(f"Email must be at most {MAX_EMAIL_LENGTH} characters.")

    roles = normalise_roles(role or [])

    generated = password is None
    if password is None:
        password = generate_password()
    check_password(password)

    with get_sessionmaker()() as session:
        if session.scalar(select(User).where(User.email == email)) is not None:
            fail(f'An account already exists for "{email}".')

        account = User(
            email=email,
            roles=roles,
            password=hash_password(password, settings.bcrypt_rounds),
        )
        session.add(account)
        try:
            session.commit()
        except IntegrityError:
            # The lookup above races with a concurrent run; the unique index on
            # email is what actually guarantees it.
            session.rollback()
            fail(f'An account already exists for "{email}".')

        typer.secho(f"[OK] Created {email}.", fg=typer.colors.GREEN)
        if generated:
            # The only time this password is visible. It is stored only as a hash.
            typer.echo(f"  Generated password: {password}")
        typer.echo(f"  Roles: {', '.join(account.all_roles())}")


@user_app.command("reset-password")
def reset_password(
    email: EmailArgument,
    password: PasswordOption = None,
    keep_sessions: Annotated[
        bool,
        typer.Option(
            "--keep-sessions",
            help="Leave existing refresh tokens valid instead of revoking them.",
        ),
    ] = False,
) -> None:
    """Set an account's password and revoke its sessions."""
    settings = get_settings()

    generated = password is None
    if password is None:
        password = generate_password()
    check_password(password)

    with get_sessionmaker()() as session:
        account = session.scalar(select(User).where(User.email == email))
        if account is None:
            fail(f'No account found for "{email}".')
            return  # unreachable; fail() exits, and this tells the type checker so

        account.password = hash_password(password, settings.bcrypt_rounds)

        # A reset that leaves refresh tokens alive is incomplete: a stolen token
        # would keep minting JWTs for the rest of its lifetime.
        revoked = 0
        if not keep_sessions:
            revoked = session.execute(
                delete(RefreshToken).where(RefreshToken.user_id == account.id)
            ).rowcount
        session.commit()

        typer.secho(f"[OK] Password updated for {email}.", fg=typer.colors.GREEN)
        if generated:
            typer.echo(f"  Generated password: {password}")
        if keep_sessions:
            typer.secho(
                "[WARNING] Existing sessions were left active (--keep-sessions).",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo(f"  Refresh tokens revoked: {revoked}")
