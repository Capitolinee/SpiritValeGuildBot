# 🏰 SpiritVale Guild Bot

Guild loot tracking by hand is a pain — who showed up, what dropped, who gets paid, who's already claimed. A raid ends and it's all buried in chat, and good luck reconstructing it the next day.

This bot exists to fix that. Send it a screenshot of the party and it reads out who was there. Loot drops, you log it with a command. It figures out who gets paid what, tracks who's claimed, and writes all of it into a Google Sheet — open it anytime, or just let the bot run the show.

## What it does

- 📸 Reads party rosters and loot drops from screenshots, or skip the screenshot and just type it in
- 💰 `!loot` handles the whole "sell it and split, give it to someone free, or hold it for the guild" decision in one place
- 🧾 `!claim` lets everyone grab their own cut instead of an officer wiring money around
- 🧑 Members register their own character, class, and availability — no manual roster to maintain
- 📊 Everything lives in a Google Sheet, so it's transparent and anyone can check it
- 📝 Every action gets an audit log entry, so disputes have a paper trail

Data lives in Google Sheets on purpose, not a database — officers don't need to learn SQL, and anyone can open the sheet and understand what happened, or tweak it by hand if needed.

## Getting started

```bash
git clone this-repo
cd SpiritValeGuildBot
pip install -r requirements.txt
python bot.py
```

You'll need to set a few environment variables before it'll actually connect (Discord token, a couple of API keys). See `store.py`'s header comment for the exact sheet layout it expects.

Once it's running, you'll see "機器人已順利上線" in the console. Type `!help` in Discord to see everything it can do.

## A few commands to get a feel for it

```
!startsession 熊爺,柒柒,Open匠   # start a session without a screenshot
!loot                            # pick an item, decide what to do with it
!claim                           # claim your own share
!profile                         # register your character
```

There's a lot more — job trees, channel permissions, audit log lookup — all covered by `!help`.

## License

See [LICENSE](./LICENSE).
