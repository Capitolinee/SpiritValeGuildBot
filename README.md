# 🏰 SpiritVale Guild Bot

Guild loot tracking by hand is a pain — who showed up, what dropped, who gets paid, who's already claimed. A raid ends and it's all buried in chat, and good luck reconstructing it the next day.

This bot exists to fix that. Send it a screenshot of the party and it reads out who was there. Loot drops, you log it. It figures out who gets paid what, tells everyone who to collect from, tracks who's claimed, and writes all of it into a Google Sheet — open it anytime, or just let the bot run the show.

## What it does

- 📸 Reads party rosters and loot drops from screenshots — only in the channels you pick, so random memes don't burn through the recognition quota
- 💰 `/loot` handles the whole "sell it and split, give it to someone free, or hold it for the guild" decision in one place
- 💵 `/claim` lets everyone grab their own cut, grouped by who's paying out, so nobody has to ask "wait, who do I get this from?"
- 🔘 Button panels you can post in any channel — members register characters, set availability, sell loot, and claim money without memorizing a single command
- 🧑 Members manage their own characters and availability — no roster for officers to babysit
- 📊 Everything lives in a Google Sheet, so it's transparent and anyone can check it
- 🛡️ Anything that moves or deletes data backs itself up first, checks the result, and rolls back if something looks off
- 🔁 A redeploy in the middle of a raid doesn't lose the session — the bot picks up where it left off
- 📝 Every action gets an audit log entry, so disputes have a paper trail

Data lives in Google Sheets on purpose, not a database — officers don't need to learn SQL, and anyone can open the sheet and understand what happened, or tweak it by hand if needed.

## Getting started

```bash
git clone this-repo
cd SpiritValeGuildBot
pip install -r requirements.txt
python bot.py
```

You'll need to set a few environment variables before it'll actually connect (Discord token, a couple of API keys). See `store.py`'s header comment for the exact sheet layout it expects — the bot creates its own settings tabs the first time it needs them.

Once it's running, you'll see "機器人上線" in the console. A good first few minutes in Discord:

```
/ocrchannel action:開啟          # in your raid channel: only read screenshots here
/postpanel                       # post the member panel (characters, availability)
/postpanel panel:寶物結算         # post the loot panel (sell, claim, who's been paid)
```

## A few commands to get a feel for it

```
/startsession names:熊爺,柒柒,Open匠   # start a session without a screenshot
/item name:死靈卡                      # log a drop by hand
/loot                                 # pick an item, decide what to do with it
/claim                                # claim your own share
/unclaimed                            # see who's been paid for an item and who hasn't
```

Officers also get a small toolbox for keeping the sheet healthy — checking for characters linked to the wrong account, fixing them, repairing formulas, and deleting a mistyped record. It's all in `/help`, and those commands only show up for people with Manage Server.

Everything runs as slash commands, and most replies are only visible to you, so channels stay clean. The session and loot recording commands (`startsession`, `noloot`, `item`, `items`, `donate`, `loot`, `sell`, `giveto`) also accept the `!` prefix, since those get typed fast and often during a raid.

## License

See [LICENSE](./LICENSE).
