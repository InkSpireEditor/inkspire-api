<p align="center">
  <img src="assets/logo.png" alt="InkSpire Logo" width="200"/>
</p>

# InkSpire API

## ⚠️ WARNING: Under Heavy Development ⚠️

**This project is currently under active and heavy development. It is NOT ready for general use and may contain bugs, incomplete features, or breaking changes. Use at your own risk.**

![CI](https://github.com/InkSpireEditor/inkspire-api/actions/workflows/ci.yml/badge.svg?branch=main)

This is the backend API for InkSpire, a modern web-based text editor. It provides all the necessary services for the [InkSpire Frontend](../inkspire-frontend) to function.

---

## ✨ Features

- **RESTful API**: Provides a complete set of endpoints for file and directory management.
- **JWT Authentication**: Secures the API using JSON Web Tokens for stateless authentication.

---

## 🧠 Technology Stack

- [Symfony](https://symfony.com/) — A set of reusable PHP components and a PHP framework to build web applications.
- [PHP](https://www.php.net/) 8.2+

---

## 🚀 Getting Started

### Prerequisites

- [PHP](https://www.php.net/) 8.2 or later
- [Composer](https://getcomposer.org/)
- [Symfony CLI](https://symfony.com/download)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd inkspire-api
    ```

2.  **Install dependencies:**
    ```bash
    composer install
    ```

3.  **Set up environment variables:**
    Create a `.env` file and configure your database connection and other variables. You will need to generate the JWT keys.
    ```bash
    php bin/console lexik:jwt:generate-keypair
    ```
    This will generate `config/jwt/private.pem` and `config/jwt/public.pem` and update your `.env` file.

5.  **Run database migrations:**
    ```bash
    php bin/console doctrine:database:create --env=dev
    php bin/console doctrine:database:create --env=test
    php bin/console doctrine:migrations:migrate --env=dev
    php bin/console doctrine:migrations:migrate --env=test
    php bin/console doctrine:fixtures:load
    ```

6. **Create file storage folder:**
    ```bash
    mkdir var/files
    ```

7.  **Start the server:**
    ```bash
    symfony server:start
    ```

The API will be running at `http://127.0.0.1:8000`.

---

## 🐍 The Python API (in progress, this branch only)

This branch carries a rewrite of the API in Python — FastAPI, SQLAlchemy and Alembic
— following the design in the umbrella repository's `ARCHITECTURE.md`. It is not
finished and is not on `main`. The Symfony application above is still the released
one, and both run from this working tree in the meantime.

Working so far: authentication, access control on `/api`, and account management from
the shell. Not yet written: text generation and the file and directory routes.

```bash
poetry install

# A signing secret is required and has no default.
echo "INKSPIRE_JWT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')" >> .env.local

poetry run alembic upgrade head      # creates `user` and `refresh_token`
poetry run inkspire user create you@example.com
poetry run uvicorn inkspire_api.main:app --port 8000 --reload

poetry run pytest
```

The default database is `var/data_dev.db`. A database that already holds `user` and
`refresh_token` tables satisfies the first revision as it stands, so record it as
applied rather than running it:

```bash
poetry run alembic stamp 9755af1d75c1
```

### Accounts

There is no registration endpoint. Accounts are made and reset from the shell:

```bash
poetry run inkspire user create alice@example.com                      # prints a generated password
poetry run inkspire user create alice@example.com -p '...' -r ROLE_ADMIN
poetry run inkspire user reset-password alice@example.com              # also revokes refresh tokens
poetry run inkspire user reset-password alice@example.com --keep-sessions
```

### Authentication

`POST /auth` takes `{"username", "password"}` and answers `{"token"}`, setting three
`SameSite=Strict` cookies: `jwt_token` (httpOnly, path `/`), `refresh_token`
(httpOnly, path `/auth`, so it is not sent with ordinary API calls) and a readable
`auth_status=1` that lets a browser client tell it has a session without reading the
token. `POST /auth/refresh` issues a new JWT and rotates the refresh token;
`POST /auth/logout` deletes it and clears all three cookies.

A request to `/api` authenticates with either the `jwt_token` cookie or an
`Authorization: Bearer` header. Tokens are signed HS256 with `INKSPIRE_JWT_SECRET`;
nothing outside this application verifies one, so there is no keypair to manage.

Errors are `{"code", "message"}` at every status.

---

## 🧪 API Endpoints

A Postman collection or OpenAPI/Swagger documentation will be available soon. Here are the main endpoints:

### Auth
- `POST /auth`: Authenticate and receive a JWT.

### Files & Directories
- `GET /api/tree`: Get the full file and directory structure for the user.
- `POST /api/file`: Create a new file.
- `GET /api/file/{id}`: Get details for a specific file.
- `PUT /api/file/{id}`: Update a file's details (e.g., name, parent directory).
- `DELETE /api/file/{id}`: Delete a file.
- `POST /api/dir`: Create a new directory.
- `GET /api/dir/{id}`: Get details for a specific directory and its contents.
- `PUT /api/dir/{id}`: Update a directory's details (e.g., name, summary).
- `DELETE /api/dir/{id}`: Delete a directory.

### Text Generation
- `POST /api/ollama/generate`: Get a completion for the provided text using the provided model.

---

## 🧪 Quality and Testing

This project uses **AI-assisted development** for rapid implementation, with human oversight and validation. Automated tests are in place to ensure API correctness and reliability.

Run the test suite:
```bash
php bin/phpunit
```

---

## 📜 License

This project is released under the [MIT License](../inkspire-frontend/LICENSE).
It is provided *as is*, without warranty, but every effort is made to ensure code reliability and responsible use of AI-generated components.

---

## 💬 Acknowledgments

InkSpire is based on a code developped by:

- [Evann Abrial](https://www.linkedin.com/in/evann-abrial-26b446297/)
- [Lola Chalmin](https://www.linkedin.com/in/lola-chalmin-112ab9290/)
- [Roxane Rossetto](https://www.linkedin.com/in/roxane-rossetto-3b9158211/)

---

*© 2025 InkSpire. Built with care, code, and a bit of inkSpiration.*
