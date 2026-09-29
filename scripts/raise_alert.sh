#!/usr/bin/env bash
# Open, or comment on, the GitHub Issue for a pipeline alert (Phase 1.1).
# GitHub emails the repository owner, who is @mentioned in every alert.
#
# Used by .github/workflows/pipeline-alert.yml and data-freshness.yml.
# Inputs arrive ONLY through environment variables and are validated here;
# nothing is interpolated into shell code by the workflow.
#   ALERT_KIND ALERT_DATE ALERT_DETAIL ALERT_REQUEST_ID ALERT_OWNER RUN_URL
#   GH_TOKEN (issues: write)  GITHUB_REPOSITORY
set -euo pipefail

KINDS="nse_blocked stale_dispatched dispatch_failed token_expiring github_unreadable stale_data check_failed test_alert"

kind="${ALERT_KIND:-}"
day="${ALERT_DATE:-}"
case " $KINDS " in *" $kind "*) ;; *) echo "refusing unknown alert kind: '$kind'" >&2; exit 1 ;; esac
[[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "refusing bad date: '$day'" >&2; exit 1; }
[[ "${ALERT_OWNER:-}" =~ ^[A-Za-z0-9-]+$ ]] || { echo "refusing bad owner" >&2; exit 1; }

# One line, bounded, no mentions or code fences.
detail=$(printf '%s' "${ALERT_DETAIL:-}" | tr -d '@`' | tr '\000-\037' ' ' | cut -c1-300)
request_id=$(printf '%s' "${ALERT_REQUEST_ID:-}" | tr -cd 'A-Za-z0-9-' | cut -c1-40)

case "$kind" in
  nse_blocked)       what="Cloudflare could not reach NSE (403/429/5xx/network). The pipeline was dispatched anyway; it fetches NSE from GitHub's runners." ; check="Check today's daily-data-update run. Repeated blocks mean NSE/Akamai is blocking Cloudflare." ;;
  stale_dispatched)  what="NSE published data for this date but no successful load existed at the staleness check. An emergency run was dispatched." ; check="Check that run and any earlier failed runs for the date." ;;
  dispatch_failed)   what="The Worker could not dispatch the daily workflow." ; check="Most likely GH_DISPATCH_TOKEN expired, was revoked, or lost Actions write. The GitHub fallback (~14:17 UTC cron, runs hours late) still covers the day." ;;
  token_expiring)    what="The Cloudflare Worker's GitHub dispatch token is about to expire." ; check="Create a fine-grained token (this repository only; Actions: Read and write), replace the GH_DISPATCH_TOKEN secret, then run Cloudflare Worker -> deploy-and-test." ;;
  github_unreadable) what="The Worker could not read GitHub Actions runs." ; check="Token problem or GitHub outage. See the Worker logs in Cloudflare." ;;
  stale_data)        what="The independent freshness check found a published trading day that is not loaded in Neon." ; check="See the freshness run log for the dates; run Daily data update manually to catch up." ;;
  check_failed)      what="The freshness check itself failed or could not verify a date." ; check="See the freshness run log." ;;
  test_alert)        what="Test alert - the alert path works." ; check="Close this issue." ;;
esac

title="[pipeline-alert] $kind $day"
body=$(printf '@%s\n\n**%s**\n\n- What to check: %s\n- Detail: %s\n- Request id: %s\n- Run: %s\n\n_Opened automatically by the data pipeline. Close when resolved._\n' \
  "$ALERT_OWNER" "$what" "$check" "${detail:-none}" "${request_id:-none}" "${RUN_URL:-n/a}")

# Title is built only from validated parts, so it is safe inside the jq string.
existing=$(gh issue list --repo "$GITHUB_REPOSITORY" --state open --limit 100 \
  --json number,title --jq ".[] | select(.title == \"$title\") | .number" | head -n1)
if [ -n "$existing" ]; then
  gh issue comment "$existing" --repo "$GITHUB_REPOSITORY" --body "$body"
  echo "alert: commented on #$existing ($title)"
else
  gh issue create --repo "$GITHUB_REPOSITORY" --title "$title" --body "$body"
  echo "alert: opened ($title)"
fi
