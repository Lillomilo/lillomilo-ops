# Setting up Lillomilo Ops Tool on GitHub

## 1. Create a private repository
1. Go to github.com and click the **+** in the top right → **New repository**.
2. Name it something like `lillomilo-ops` (or "lillomilo-ops-tool").
3. Set visibility to **Private** (important — this keeps your setup out of public view even though secrets themselves are stored encrypted regardless).
4. Click **Create repository**.

## 2. Upload the files
1. On your new (empty) repo's page, click **"uploading an existing file"** (or drag-and-drop).
2. Drag in these three files/folders from the zip you were sent:
   - `check_orders.py`
   - `requirements.txt`
   - `state.json`
   - `.github/workflows/order-alerts.yml` (GitHub's uploader supports dragging in the whole `.github` folder — if it doesn't accept folders in your browser, create the file manually: click "Create new file", type `.github/workflows/order-alerts.yml` as the filename — GitHub will automatically create the folders — and paste in its contents.)
3. Commit the files (the green "Commit changes" button).

## 3. Add your secrets
Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add these four, one at a time (name exactly as shown, value from your `.env` file / what you gave me earlier):

| Secret name | Value |
|---|---|
| `AMAZON_LWA_CLIENT_ID` | your Client ID |
| `AMAZON_LWA_CLIENT_SECRET` | your Client Secret |
| `AMAZON_REFRESH_TOKEN` | your Refresh Token |
| `SLACK_WEBHOOK_URL` | your Slack webhook URL |

These are encrypted by GitHub — even you won't be able to view them again after saving, only replace them if needed.

## 4. Test it manually
1. Go to the **Actions** tab of your repo.
2. You should see a workflow called **"Amazon Order Slack Alerts"** in the left sidebar — click it.
3. Click **"Run workflow"** (dropdown button) → **Run workflow** again to confirm.
4. Wait ~30-60 seconds, refresh, and click into the run that appears. Green checkmark = it worked. Click into the "Check for new orders and notify Slack" step to see the log output (it'll say how many orders it found).
5. Check your Slack channel — if there were any real orders in the last 20 minutes, you should see a message.

## 5. Let it run automatically
Once the manual test works, you don't need to do anything else — the schedule in `order-alerts.yml` runs it every 15 minutes automatically, forever, for free (GitHub gives free accounts thousands of minutes/month of Actions time, and this uses well under a minute per run).

## Troubleshooting
- **Red X on the run** — click into it, then into the failing step, and share the error text — most issues are a typo'd secret value.
- **No Slack message but the run succeeded** — probably just means no orders were placed in that window. That's normal, not a bug.
- **"invalid_client" or "invalid_grant" errors** — usually a secret was pasted with extra spaces/line breaks, or the refresh token needs to be re-generated (they can expire if unused for a long time, or if the app's authorization is revoked).
