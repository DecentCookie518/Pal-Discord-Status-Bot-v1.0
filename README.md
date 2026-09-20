# Palworld Discord status bot

This bot updates **one existing Discord embed** with the current Palworld server information and player list. It is suitable for a Render Web Service: the included `/health` endpoint lets Render verify that the process is alive. It does not prevent a free Render service from sleeping.

## What it requests

The bot calls these read-only official Palworld REST endpoints every `UPDATE_INTERVAL` seconds:

- `GET /v1/api/info` — server name, description, version and world GUID.
- `GET /v1/api/players` — the currently connected player list.
- `GET /v1/api/metrics` — server FPS, frame time, uptime, player capacity, base-camp count and in-game day.

Palworld documents these endpoints and HTTP Basic Authentication in its [REST API guide](https://docs.palworldgame.com/category/rest-api/). The API must be enabled in the server configuration (`RESTAPIEnabled=True`) and should not be exposed publicly without suitable network protection. With `PALWORLD_API_PASSWORD` set, the bot sends HTTP Basic credentials using username `admin` and that password.

## Discord setup

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications), then copy its token.
2. Invite the bot to your server with permissions to **View Channel**, **Send Messages**, **Embed Links**, **Read Message History**, and **Manage Messages**. `Manage Messages` is optional if it can edit its own messages, but is commonly granted.
3. Enable Discord Developer Mode, right-click the target text channel, and choose **Copy Channel ID**.

`Read Message History` matters: when `STATUS_MESSAGE_ID` is blank, the bot searches its old embeds for a fixed footer marker after every restart. Thus it does not depend on Render's ephemeral filesystem and continues to update the same message. After the first start, copy the message ID from the created embed into `STATUS_MESSAGE_ID` if you prefer an explicit lookup.

## Local run

1. Copy `.env.example` to `.env` and fill it in. (The script itself deliberately does not load `.env`; export these variables through your shell or use a local environment loader.)
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python bot.py`

Example API address for REST port 27262: `http://YOUR_SERVER_HOST:27262/v1/api`. Supplying only `http://YOUR_SERVER_HOST:27262` also works; the script appends `/v1/api`.

## Deploy on Render

1. Push this folder to a Git repository.
2. In Render, select **New → Blueprint** and choose that repository, or create a **Web Service** manually.
3. If configuring manually, use build command `pip install -r requirements.txt`, start command `python bot.py`, and health-check path `/health`.
4. Set `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `PALWORLD_API_URL`, and (normally) `PALWORLD_API_PASSWORD` as environment variables. Start with `UPDATE_INTERVAL=120`; the minimum accepted value is 30 seconds.
5. Deploy and inspect the logs. The bot logs the ID of the status message it created.

Render web services must bind to the `PORT` environment variable; the bot's small health server does that automatically. Render's health checks confirm an HTTP service is responding, but they do not keep a free service awake. Consult [Render's web-service docs](https://render.com/docs/web-services) and [health-check documentation](https://render.com/docs/health-checks) for current plan behavior.

## Troubleshooting

- **401 Unauthorized:** confirm `PALWORLD_API_PASSWORD` is the Palworld server admin password and REST API access is enabled. The bot uses Basic Auth username `admin`.
- **Timeout/refused connection:** Render must be able to reach the Palworld REST endpoint. A LAN-only or private address usually cannot be reached from Render. Prefer a VPN/tunnel or host the bot on the same private network; do not publish the game admin API openly.
- **A new message appears after a restart:** grant **Read Message History**, keep `STATUS_EMBED_MARKER` unchanged, or set the original message ID in `STATUS_MESSAGE_ID`.
- **Missing embeds:** grant **Embed Links** and make sure the bot can view and send in the selected channel.
