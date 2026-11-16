# Ursa-kingshot

*A Discord bot + website for Kingshot alliances.*

A Discord bot + website for your Kingshot alliance servers:

- **Gift codes** - finds new codes automatically and redeems them for every linked account
- **Reminders** - Bear Trap (Group 1 and Group 2), Arena reset and any custom event. Alerts are rich
  Discord embeds with a countdown picture, a live "starts in …" timer and the time in each member's own time zone
- **Timeline** - every event and alert for the next 7 days
- **Battle plans** - build Castle / KvK / Bear Trap squads (leader, joiners, target, time) and post them as an image
- **KvK comparison** - paste both kingdoms' top players and power; get totals, rank-by-rank matchups and a verdict
- **Officer access** - give a role (e.g. R4) or a member view/edit access to chosen sections, without Discord admin
- **Privacy** - recent claims and activity are admins/officers-only by default; members can be allowed to see them, with names masked
- **Public page** - anyone can see active gift codes without logging in (no player data)
- **Multi-server** - each Discord server has its own players, reminders, plans and settings
- **Log in with Discord** - the website shows the servers you manage and your own accounts

```
             ┌──────────────────── one Python process ─────────────────────┐
 Browser ───►│ website (login with Discord)                                 │
 Discord ───►│ slash commands · role buttons · reminder alerts              │
             │ gift-code engine ──────────────────────────────────────────► │──► Kingshot gift-code site
             │ every hour: check code feed + redeem                         │◄── kingshot.net code feed
             │                       SQLite (data/kingshot.db)              │
             └──────────────────────────────────────────────────────────────┘
```

## Website and Discord do the same things

Everything can be managed from **either** the website or Discord. The website's **Help** page
(and `/help` in Discord) shows both side by side.

| Task | Website | Discord | Who |
|---|---|---|---|
| Link / unlink my account | Home | `/add`, `/remove` | everyone |
| My accounts, history | Home | `/myaccounts`, `/history` | everyone |
| List codes / reminders | Gift codes | `/code list`, `/reminder list` | everyone |
| Auto-create category, channels, alert roles | Server → Settings → Auto setup | `/autosetup` | managers |
| Report channel, manager role | Server → Settings | `/setup`, `/settings` | managers |
| Players: add, list, edit, pause, resume, remove | Server → Players | `/add`, `/players`, `/player edit\|pause\|resume`, `/remove` | managers |
| Reminders: add, edit, pause, resume, test, delete | Server → Reminders | `/reminder add\|edit\|pause\|resume\|test\|remove` | managers |
| Alert role buttons | Server → Reminders | `/reminder rolebuttons` | managers |
| Add code, redeem now, check connection | Gift codes | `/code add`, `/code redeem-now`, `/code check-salt` | managers |
| Timeline | Server → Timeline | `/timeline` | everyone |
| Battle plans | Server → Battle plans | `/plan list\|show\|post` | members view, officers edit |
| KvK comparison | Server → KvK | `/kvk list\|show\|post` | members view, officers edit |
| Recent claims | Server → Overview | `/claims` | admins/officers (members if allowed) |
| Officer access | Server → Settings | `/access grant\|list` | admins |
| Privacy | Server → Settings | `/privacy` | admins |
| Delete / retry a code | Gift codes | `/code remove`, `/code retry` | owner |

**Owner** = user IDs in `OWNER_IDS`. **Managers** = people with Manage Server, or the server's
bot-manager role. Members can also edit their own account's kingdom with `/player edit`.

## Reminders

- Times are **UTC** (Kingshot's game time).
- **Two Bear Traps:** Group 1 and Group 2 each have their own reminder and role.
- Defaults: **Bear Trap** every 48h, alerts 30, 10 and 0 min before. **Arena** every 24h, alerts 60
  and 15 min before. Check these against your game and change them if needed.
- Alerts are **always posted in the channel**. If the reminder has a role, that role is pinged too.
  Nobody has to pick the role for the message to appear.
- Role buttons let members opt in and out of pings. The bot needs **Manage Roles**, and its own role
  must be **above** the alert roles in Server Settings -> Roles.
- If the bot was offline, alerts more than 5 minutes late are skipped, so nobody gets spammed with old pings.

## Setup

### Discord application
1. https://discord.com/developers/applications -> your app.
2. **Bot** -> copy the token -> `DISCORD_TOKEN`.
3. **OAuth2** -> copy **Client ID** -> `DISCORD_CLIENT_ID`. **Reset Secret** -> copy -> `DISCORD_CLIENT_SECRET`.
4. **OAuth2 -> Redirects** -> add `http://localhost:8080/auth/callback` (and your Azure address later).
5. **OAuth2 -> URL Generator** -> scopes `bot` + `applications.commands`. Permissions:
   **View Channels, Send Messages, Embed Links, Manage Roles, Manage Channels**. Open the link and invite the bot to each server.

### Run locally
```bash
cp .env.example .env      # fill it in
docker compose up -d --build
```
Open http://localhost:8080.

### Run on Azure
See [DEPLOY-AZURE.md](DEPLOY-AZURE.md).

### Tests
```bash
python -m unittest discover tests
```

## Things to know
- **Kingdom is required.** Century Games removed the player lookup, so each account is saved
  as ID + kingdom. A wrong kingdom pauses that account, and the website shows it.
- **Be gentle.** `REQUEST_DELAY` (3s) spaces out requests.
- **It can break.** There's no official API, and automating the site may be against Century
  Games' terms. If they change the salt or add a captcha, gift codes pause, the owner gets a DM,
  and reminders keep working.
