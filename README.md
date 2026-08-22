# Rijswijk Apartment Watcher

Checks Pararius, VBT, and Funda every 15 minutes and sends you a Telegram
message the moment a new rental listing appears under €2000, roughly within
10km of Rijswijk. Runs forever on GitHub's free Actions scheduler - no
computer of yours needs to stay on.

## What's NOT covered, and why

- **Vesteda** - its listings load via JavaScript after the page opens, so a
  simple scraper sees nothing. Not included.
- **Frisia Makelaars** - their site's `robots.txt` explicitly blocks
  automated access, so this respects that and skips them. Register directly
  on their site for alerts instead.
- **Funda** - included, but real estate sites like Funda actively try to
  block bots over time (especially from cloud servers like GitHub's). It may
  work fine for a while and then start failing. If it stops finding
  anything, check the Actions logs (see below) - Funda's own free "bewaar
  zoekopdracht" (save search) email alert is a solid fallback.

## One-time setup (about 10 minutes)

### 1. Create a Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts.
2. It gives you a **token** that looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxx`. Save it.
3. Send your new bot any message (e.g. "hi") so it knows about you.
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   (replace `<YOUR_TOKEN>`). Find `"chat":{"id":123456789,...}` in the
   response - that number is your **chat ID**. Save it.

### 2. Create a GitHub repo
1. Go to [github.com/new](https://github.com/new), create a new repo (private is fine).
2. Upload these files, **keeping the folder structure exactly as-is**:
   ```
   scrape.py
   requirements.txt
   seen_listings.json
   README.md
   .github/workflows/watch.yml
   ```
   Easiest way: on the repo page, use "Add file" → "Upload files" and drag
   the whole folder in (GitHub preserves the `.github/workflows/` path).

### 3. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN` → the token from step 1
- `TELEGRAM_CHAT_ID` → the chat ID from step 1

### 4. Run it once manually
Go to the **Actions** tab → click "Apartment Watcher" → **Run workflow**.
This first run just records today's listings as the baseline (you won't get
flooded with every existing listing) - you should see 0 notifications, and
`seen_listings.json` will get updated in the repo.

From then on, it runs automatically every 15 minutes and messages you only
for genuinely new listings.

## Tuning it

All the settings you'd want to change live at the top of `scrape.py`:

- **`MAX_PRICE`** - change 2000 to whatever you want.
- **`SOURCES`** - the URLs it checks. The most reliable way to adjust the
  radius/price filters is to open the site yourself, set the filters exactly
  how you want (e.g. click the "+10 km" button), then copy the resulting URL
  from your browser's address bar and paste it in here.
- **`NEARBY_TOWNS`** - since VBT doesn't support a radius filter in its URL,
  it's filtered by matching these town names instead. Add or remove towns.

## Checking it's still working

Repo → **Actions** tab → click any run → open the "Run scraper" step. It
prints how many listings it found per site on every run, so you can spot if
one of them suddenly returns 0 (usually means the site changed its layout
or started blocking requests).
