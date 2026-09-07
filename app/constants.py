"""Fixed identifiers shared between migrations, app code, and eval scripts."""

import uuid

# Owner assigned to documents that predate M3 authentication (rows that had
# user_id = 'local'). Fixed so migration 0003, the app, and eval scripts all
# agree on it. The account is intentionally NOT loginable: its password hash is
# a sentinel that can never verify, so nothing can authenticate as it. Eval
# scripts reach these documents through the database, not the API.
SEED_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000dead")
SEED_USER_EMAIL = "seed@local.invalid"

# Not a valid argon2 encoded hash, so verification always fails.
UNUSABLE_PASSWORD_HASH = "!unusable"
