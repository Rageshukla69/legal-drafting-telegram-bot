# Telegram Authorization

The bot is private by default. Access is controlled by the owner's Telegram numeric user ID plus a persistent allowlist stored in the same Cosmos `cases` container (partition `__auth__`). This avoids requiring a second Cosmos container/throughput allocation.

## Heroku
Set this Config Var before deploying:

`OWNER_TELEGRAM_ID=<your numeric Telegram user ID>`

The owner can get their ID with `/myid` in the bot. Do not put the ID in Git if you prefer to keep it out of the repository.

## Commands

- `/myid` — any Telegram user can see their own numeric ID.
- `/authorize <user_id>` — owner only; add a user to the allowlist.
- `/unauthorize <user_id>` — owner only; remove a user.
- `/authorized` — owner only; show authorized user IDs.

The owner is always authorized and cannot be removed.

## Security behavior

Authorization is checked for `/start`, `/newcase`, `/cancel`, `/summary`, text/voice intake, and inline-button callbacks. Unauthorized users cannot create cases or generate drafts.

Because authorization records live in Cosmos DB, they survive Heroku dyno restarts and deployments. The SQLite fallback also stores them locally for development.
