#!/usr/bin/env bash
# One-time setup for the live-validation workflow (docs/live-validation.md), in one command:
#
#   az login && gh auth login                 # as someone who can create app registrations and role assignments
#   scripts/setup_live_validation.sh --subscription <id> --repo <owner>/maf-golden-path [--alert-email you@x.com]
#   gh workflow run live-validation.yml --repo <owner>/maf-golden-path && gh run watch --repo <owner>/maf-golden-path
#
# It creates (or reuses, by name) two Entra app registrations and a federated credential, grants the workflow
# identity Owner on the subscription, registers the resource providers the platform needs, and creates the
# GitHub environment "live" with its variables. No secrets are created anywhere. Safe to run again.
set -euo pipefail

SUBSCRIPTION=""
REPO=""
ALERT_EMAIL=""
WORKFLOW_APP_NAME="agentkit-live-validation"
API_APP_NAME="agentkit-live-api"

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

need_value() { if [ $# -lt 2 ] || [ -z "$2" ]; then echo "$1 needs a value" >&2; usage 1; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --subscription) need_value "$@"; SUBSCRIPTION="$2"; shift 2 ;;
    --repo) need_value "$@"; REPO="$2"; shift 2 ;;
    --alert-email) need_value "$@"; ALERT_EMAIL="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done
if [ -z "$SUBSCRIPTION" ] || [ -z "$REPO" ]; then usage 1; fi

step() { printf '\n==> %s\n' "$*"; }
run() { "$@"; }

step "Checking the Azure CLI and GitHub CLI logins"
command -v az >/dev/null || { echo "Install the Azure CLI: https://aka.ms/azcli" >&2; exit 1; }
command -v gh >/dev/null || { echo "Install the GitHub CLI: https://cli.github.com" >&2; exit 1; }
az account show --output none 2>/dev/null || { echo "Run 'az login' first." >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first." >&2; exit 1; }
az account set --subscription "$SUBSCRIPTION"
TENANT=$(az account show --query tenantId --output tsv)
echo "subscription $SUBSCRIPTION, tenant $TENANT, repo $REPO"

app_id() {  # the appId of the app registration with this display name, created if missing
  local name="$1" id
  # exact name (--display-name alone is a prefix match)
  id=$(az ad app list --filter "displayName eq '$name'" --query "[0].appId" --output tsv)
  if [ -z "$id" ]; then
    id=$(az ad app create --display-name "$name" --sign-in-audience AzureADMyOrg --query appId --output tsv)
    echo "created app registration $name ($id)" >&2
  else
    echo "reusing app registration $name ($id)" >&2
  fi
  az ad sp show --id "$id" --output none 2>/dev/null || az ad sp create --id "$id" --output none
  echo "$id"
}

step "Workflow identity: $WORKFLOW_APP_NAME (federated credential for the 'live' environment, no secret)"
APP=$(app_id "$WORKFLOW_APP_NAME")
SUBJECT="repo:$REPO:environment:live"
if az ad app federated-credential list --id "$APP" --query "[?subject=='$SUBJECT'] | length(@)" --output tsv | grep -qx 0; then
  run az ad app federated-credential create --id "$APP" --parameters "{
    \"name\": \"live\", \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$SUBJECT\", \"audiences\": [\"api://AzureADTokenExchange\"]}" --output none
fi
step "Owner on the subscription for the workflow identity (it creates resource groups and role assignments)"
SP_OBJECT_ID=$(az ad sp show --id "$APP" --query id --output tsv)
if [ "$(az role assignment list --assignee "$SP_OBJECT_ID" --role Owner --scope "/subscriptions/$SUBSCRIPTION" --query 'length(@)' --output tsv)" = "0" ]; then
  # by object id and principal type: works even before the new service principal has replicated in Entra
  run az role assignment create --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
    --role Owner --scope "/subscriptions/$SUBSCRIPTION" --output none
fi

step "Easy Auth app for the sample and the fleet view: $API_APP_NAME"
API=$(app_id "$API_APP_NAME")
run az ad app update --id "$API" --identifier-uris "api://$API" --enable-id-token-issuance true

step "Resource providers the platform uses (registration is idempotent and can take a few minutes)"
for ns in Microsoft.App Microsoft.ApiManagement Microsoft.CognitiveServices Microsoft.DocumentDB Microsoft.Search \
          Microsoft.OperationalInsights Microsoft.Insights Microsoft.ContainerRegistry Microsoft.BotService \
          Microsoft.ManagedIdentity Microsoft.Storage Microsoft.ResourceGraph; do
  run az provider register --namespace "$ns" --output none
done

step "GitHub environment 'live' and its variables"
run gh api --method PUT "repos/$REPO/environments/live" --silent
setvar() { run gh variable set "$1" --env live --repo "$REPO" --body "$2"; }
setvar AZURE_CLIENT_ID "$APP"
setvar AZURE_TENANT_ID "$TENANT"
setvar AZURE_SUBSCRIPTION_ID "$SUBSCRIPTION"
setvar LIVE_AUTH_CLIENT_ID "$API"
[ -n "$ALERT_EMAIL" ] && setvar LIVE_ALERT_EMAIL "$ALERT_EMAIL"

cat <<EOF

Done. Start a run (about 60–90 minutes, a few US dollars of Azure usage; everything is deleted at the end):

  gh workflow run live-validation.yml --repo $REPO
  gh run watch --repo $REPO

Optional: LIVE_EVAL_USERS for the permission-trimmed knowledge cases (docs/live-validation.md).
EOF
