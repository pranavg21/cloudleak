# Running CloudLeak on Windows

Start to finish, assuming a fresh PC. About 15 minutes the first time, then
about 30 seconds every time after.

---

## Step 1 — Install two things (one time only)

**Python** — https://www.python.org/downloads/

> On the first install screen, tick the box that says **"Add python.exe to
> PATH"** at the bottom. It's easy to miss and everything fails without it.

**Node.js** — https://nodejs.org

> Take the button marked **LTS**. Accept all the defaults.

Restart your computer after installing both. (Not strictly required, but it
avoids the most common "command not found" problem.)

### Check they worked

Open **Command Prompt** (press Start, type `cmd`, hit Enter) and run:

```
python --version
node --version
```

You should see a version number from each, like `Python 3.12.4`. If instead you
see "not recognized," Python or Node didn't get added to PATH — reinstall and
make sure that box is ticked.

---

## Step 2 — Unzip the project

Unzip `cloudleak.zip` somewhere simple, like `C:\cloudleak`.

Avoid OneDrive folders and your Desktop if it's synced — file syncing can lock
files while the app is running.

---

## Step 3 — Start it

Open the `cloudleak` folder. Right-click **`run.ps1`** and choose
**"Run with PowerShell."**

The first run installs everything it needs, so give it a few minutes. It'll
print progress as it goes, then open your browser automatically.

**If Windows blocks the script** (a message about execution policy), open
PowerShell in that folder and run this once:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run.ps1
```

That permission applies only to that one window and resets when you close it.

Two black command windows will open and stay open. That's normal — that's
CloudLeak running. **Closing them stops the app.**

When it's ready, go to **http://localhost:3000**.

---

## Step 4 — Run your first audit

1. On the page, click **"Choose a file."**
2. Navigate to the `samples` folder inside `cloudleak`.
3. Pick **`azure_cost_export.csv`**.

You'll land on a report showing about **16% waste**, a list of the wasteful
resources, and the exact commands to delete them.

Try `aws_cur_export.csv` and `gcp_billing_export.csv` too — the app works out
which cloud each one came from without being told, which is a nice thing to
show off live.

---

## Stopping and restarting

- **To stop:** close the two black command windows.
- **To start again:** right-click `run.ps1` → Run with PowerShell. Fast now that
  everything's installed.

---

## If the script doesn't work

`run.ps1` is convenience, not necessity. These are the same steps by hand, and
they always work. You need **two Command Prompt windows**.

**Window 1 — the audit engine:**

```
cd C:\cloudleak\backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --port 8000
```

Watch for a line like this and **copy that key**:

```
WARNING cloudleak: No API keys configured. Minted a development key
for this process only: cl_dev_AbCdEf123456
```

Leave this window open.

**Window 2 — the web app:**

```
cd C:\cloudleak\frontend
copy .env.local.example .env.local
notepad .env.local
```

In Notepad, replace the placeholder after `CLOUDLEAK_API_KEY=` with the key you
copied, so it reads:

```
CLOUDLEAK_API_BASE_URL=http://localhost:8000
CLOUDLEAK_API_KEY=cl_dev_AbCdEf123456
```

Save, close Notepad, then:

```
npm install
npm run dev
```

Open **http://localhost:3000**.

> Note: that key is regenerated every time the backend restarts. If you restart
> Window 1, you must copy the new key into `.env.local` again. `run.ps1` exists
> specifically to handle this for you.

---

## Common problems

| What you see | What's wrong | Fix |
| --- | --- | --- |
| `'python' is not recognized` | Python not on PATH | Reinstall Python, tick "Add python.exe to PATH" |
| `'npm' is not recognized` | Node not installed | Install Node LTS from nodejs.org, restart |
| Script blocked / "cannot be loaded" | Windows execution policy | Run the `Set-ExecutionPolicy` command above |
| Page won't load | Web app still compiling | Wait 30 seconds, refresh |
| "not responding / start the backend" | Backend window closed or crashed | Check window 1 for a red error |
| `Address already in use` | Something's on port 8000/3000 | Close old CloudLeak windows and retry |
| "Too many audits" | You uploaded too fast | Wait 30 seconds. It's the rate limiter working correctly |
| Upload rejected | Not a `.csv` | Use a file from `samples\` |

Backend errors show up in the first command window. That's the first place to
look when something misbehaves.

---
