# Setting up your Saxo SIM token safely

**Never paste your token into a script, into chat, or into any file that
might get shared.** Set it as an environment variable instead — it stays
only on your machine.

## Windows (PowerShell) — for your current session only:

```powershell
$env:SAXO_TOKEN = "paste-your-token-here-directly-in-your-own-terminal"
```

Run this in the same PowerShell window right before you run `python saxo_client.py`
or `python main.py`. It only lasts for that terminal session — close the window
and you'll need to set it again next time. That's actually a good thing for
security (nothing persists on disk).

If you want it to persist across sessions (less secure, but more convenient
while testing), you can instead set it permanently:

```powershell
setx SAXO_TOKEN "paste-your-token-here"
```

Then close and reopen PowerShell for it to take effect.

## Testing the connection

Once the environment variable is set, run:

```
python saxo_client.py
```

This calls Saxo's simplest "who am I" endpoint to confirm the token works.
You should see `SUCCESS. Connected as: ...` printed. If you see `FAILED`,
paste me the error message (not the token) and I'll help debug it.

## Important: this 24-hour token is for TESTING ONLY

It expires in 24 hours and has to be manually replaced each time. For the
actual bot that will run unattended, we need the App Key + App Secret from
a registered Simulation Application (OAuth flow), which refreshes itself
automatically. Let's get this quick connection test working first, then
move to that permanent setup.
