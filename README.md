# vibertotg

Windows bridge: new messages from a Viber Desktop group into a Telegram forum topic.

Viber does not expose a group-read API, so this talks to the local `viber.db` of the account already signed in on this machine. No screenshots, no OCR. Minimize the window if you want; quitting Viber stops the DB from updating. Old history is left alone — only traffic after the process starts.

Python 3, Viber Desktop, bot already in the target group with topics enabled.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.env config.env
```

Put the BotFather token and the Viber chat name into `config.env`. With Viber running:

```
python bridge.py --setup-key
python bridge.py --discover
python bridge.py --test
python bridge.py
```

`--discover` waits for a message: send `/id` in the topic you actually want, not General. `run.bat` is the same with a pause at the end; `autostart.bat` waits for Viber then starts the loop.

Keep `config.env` out of git. It holds the bot token and the local database key.
