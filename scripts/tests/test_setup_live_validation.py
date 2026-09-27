"""setup_live_validation.sh against stub az and gh: what it creates, and that a second run reuses it."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "setup_live_validation.sh"

AZ = r'''#!/usr/bin/env bash
echo "az $*" >> "$LOG"
state="$STATE"
case "$*" in
  "account show --output none"|"account set"*) exit 0 ;;
  "account show --query tenantId --output tsv") echo tenant-1 ;;
  "ad app list --filter displayName eq 'agentkit-live-validation'"*) cat "$state/wf" 2>/dev/null ;;
  "ad app list --filter displayName eq 'agentkit-live-api'"*) cat "$state/api" 2>/dev/null ;;
  "ad sp show --id wf-app --query id --output tsv") echo wf-sp-oid ;;
  "ad app create --display-name agentkit-live-validation"*) echo wf-app > "$state/wf"; echo wf-app ;;
  "ad app create --display-name agentkit-live-api"*) echo api-app > "$state/api"; echo api-app ;;
  "ad sp show"*) [ -f "$state/sp-$4" ] ;;
  "ad sp create"*) touch "$state/sp-$4" ;;
  "ad app federated-credential list"*) if [ -f "$state/fic" ]; then echo 1; else echo 0; fi ;;
  "ad app federated-credential create"*) touch "$state/fic" ;;
  "role assignment list"*) if [ -f "$state/owner" ]; then echo 1; else echo 0; fi ;;
  "role assignment create"*) touch "$state/owner" ;;
esac
exit 0
'''
GH = '#!/usr/bin/env bash\necho "gh $*" >> "$LOG"\nexit 0\n'


def run(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, body in (("az", AZ), ("gh", GH)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    log = tmp_path / "log.txt"
    log.write_text("")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "LOG": str(log), "STATE": str(state)}
    proc = subprocess.run(["bash", str(SCRIPT), "--subscription", "sub-1", "--repo", "ben/maf-golden-path",
                           "--alert-email", "ben@example.com"], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return log.read_text().splitlines(), proc.stdout


def test_first_run_creates_everything_without_secrets(tmp_path):
    calls, out = run(tmp_path)
    joined = "\n".join(calls)
    assert "az ad app create --display-name agentkit-live-validation" in joined
    assert "az ad app create --display-name agentkit-live-api" in joined
    assert '"subject": "repo:ben/maf-golden-path:environment:live"' in joined
    assert ("az role assignment create --assignee-object-id wf-sp-oid --assignee-principal-type ServicePrincipal "
            "--role Owner --scope /subscriptions/sub-1") in joined
    assert "az ad app update --id api-app --identifier-uris api://api-app --enable-id-token-issuance true" in joined
    assert "az provider register --namespace Microsoft.App --output none" in joined
    assert "gh api --method PUT repos/ben/maf-golden-path/environments/live --silent" in joined
    for name, value in (("AZURE_CLIENT_ID", "wf-app"), ("AZURE_TENANT_ID", "tenant-1"), ("AZURE_SUBSCRIPTION_ID", "sub-1"),
                        ("LIVE_AUTH_CLIENT_ID", "api-app"), ("LIVE_ALERT_EMAIL", "ben@example.com")):
        assert f"gh variable set {name} --env live --repo ben/maf-golden-path --body {value}" in joined
    assert "secret" not in joined.replace("--silent", "")  # no client secrets, no gh secrets
    assert "gh workflow run live-validation.yml --repo ben/maf-golden-path" in out


def test_second_run_reuses_what_exists(tmp_path):
    run(tmp_path)
    calls, _ = run(tmp_path)
    joined = "\n".join(calls)
    assert "ad app create" not in joined and "federated-credential create" not in joined
    assert "role assignment create" not in joined and "ad sp create" not in joined


def test_missing_arguments_print_usage(tmp_path):
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 1 and "--subscription" in proc.stdout
    proc = subprocess.run(["bash", str(SCRIPT), "--repo", "a/b", "--subscription"], capture_output=True, text=True)
    assert proc.returncode == 1 and "--subscription needs a value" in proc.stderr
