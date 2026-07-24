from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _copy_executable(source: Path, destination: Path) -> None:
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o755)


def _clean_path(tmp_path: Path, *commands: str) -> Path:
    bin_dir = tmp_path / "clean-bin"
    bin_dir.mkdir()
    for command in ("dirname", "readlink", *commands):
        resolved = shutil.which(command)
        assert resolved, f"test requires {command}"
        (bin_dir / command).symlink_to(resolved)
    return bin_dir


def _write_fake_logpile(repo: Path, output: str = "logpile invoked") -> None:
    executable = repo / ".venv" / "bin" / "logpile"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!/bin/bash\nprintf '%s\\n' '{output}'\n")
    executable.chmod(0o755)


def test_logpile_wrapper_missing_uv_has_install_instructions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_executable(ROOT / "logpile.sh", repo / "logpile.sh")
    clean_path = _clean_path(tmp_path)

    result = subprocess.run(
        ["/bin/bash", str(repo / "logpile.sh"), "sync"],
        text=True,
        capture_output=True,
        env={"PATH": str(clean_path)},
        check=False,
    )

    assert result.returncode == 127
    assert "required command 'uv' was not found" in result.stderr
    assert "docs.astral.sh/uv" in result.stderr


def test_logpile_wrapper_bootstraps_with_locked_uv_sync(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_executable(ROOT / "logpile.sh", repo / "logpile.sh")
    uv_log = tmp_path / "uv.log"
    clean_path = _clean_path(tmp_path, "mkdir", "chmod")
    fake_uv = clean_path / "uv"
    fake_uv.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >\"$UV_LOG\"\n"
        "mkdir -p .venv/bin\n"
        "printf '%s\\n' '#!/bin/bash' 'printf \"uv bootstrapped\\\\n\"' "
        ">.venv/bin/logpile\n"
        "chmod 755 .venv/bin/logpile\n"
    )
    fake_uv.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(repo / "logpile.sh"), "sync"],
        text=True,
        capture_output=True,
        env={"PATH": str(clean_path), "UV_LOG": str(uv_log)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text().strip() == "sync --locked --quiet"
    assert "uv bootstrapped" in result.stdout


def test_logpile_wrapper_missing_bun_has_install_instructions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "web").mkdir()
    _copy_executable(ROOT / "logpile.sh", repo / "logpile.sh")
    _write_fake_logpile(repo)
    clean_path = _clean_path(tmp_path)

    result = subprocess.run(
        ["/bin/bash", str(repo / "logpile.sh"), "serve"],
        text=True,
        capture_output=True,
        env={"PATH": str(clean_path)},
        check=False,
    )

    assert result.returncode == 127
    assert "required command 'bun' was not found" in result.stderr
    assert "bun.sh/docs/installation" in result.stderr


def test_logpile_wrapper_uses_frozen_bun_lockfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "web").mkdir()
    _copy_executable(ROOT / "logpile.sh", repo / "logpile.sh")
    _write_fake_logpile(repo)
    bun_log = tmp_path / "bun.log"
    clean_path = _clean_path(tmp_path, "mkdir")
    fake_bun = clean_path / "bun"
    fake_bun.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >\"$BUN_LOG\"\n"
        "mkdir -p node_modules\n"
    )
    fake_bun.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(repo / "logpile.sh"), "serve"],
        text=True,
        capture_output=True,
        env={"PATH": str(clean_path), "BUN_LOG": str(bun_log)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert bun_log.read_text().strip() == "install --frozen-lockfile --silent"
    assert "logpile invoked" in result.stdout


def test_logpile_wrapper_reconciles_existing_node_modules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "web" / "node_modules").mkdir(parents=True)
    _copy_executable(ROOT / "logpile.sh", repo / "logpile.sh")
    _write_fake_logpile(repo)
    bun_log = tmp_path / "bun.log"
    clean_path = _clean_path(tmp_path)
    fake_bun = clean_path / "bun"
    fake_bun.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >\"$BUN_LOG\"\n"
    )
    fake_bun.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(repo / "logpile.sh"), "serve"],
        text=True,
        capture_output=True,
        env={"PATH": str(clean_path), "BUN_LOG": str(bun_log)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert bun_log.read_text().strip() == "install --frozen-lockfile --silent"
    assert "logpile invoked" in result.stdout


@pytest.mark.parametrize("via_symlink", [False, True])
def test_logpile_wrapper_clean_path_direct_and_symlink_invocation(
    tmp_path: Path, via_symlink: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_executable(ROOT / "logpile.sh", repo / "logpile.sh")
    _write_fake_logpile(repo, "resolved checkout")
    clean_path = _clean_path(tmp_path)

    invocation = repo / "logpile.sh"
    if via_symlink:
        link_dir = tmp_path / "bin"
        nested = link_dir / "nested"
        nested.mkdir(parents=True)
        (nested / "logpile-link").symlink_to("../../repo/logpile.sh")
        invocation = link_dir / "logpile"
        invocation.symlink_to("nested/logpile-link")

    result = subprocess.run(
        ["/bin/bash", str(invocation), "status"],
        text=True,
        capture_output=True,
        env={"PATH": str(clean_path)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "resolved checkout" in result.stdout


def _deploy_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "deploy-checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "site").mkdir()
    _copy_executable(
        ROOT / "scripts" / "deploy_landing.sh",
        checkout / "scripts" / "deploy_landing.sh",
    )
    html = checkout / "site" / "index.html"
    html.write_text("<!doctype html><title>landing test</title>\n")
    return checkout, html


def _deploy_path(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "deploy-bin"
    bin_dir.mkdir()
    for command in ("dirname", "mktemp", "chmod", "rm"):
        resolved = shutil.which(command)
        assert resolved, f"test requires {command}"
        (bin_dir / command).symlink_to(resolved)
    (bin_dir / "python3").symlink_to(sys.executable)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/bash\nexit 0\n")
    sleep.chmod(0o755)
    return bin_dir


def _write_fake_curl(bin_dir: Path) -> None:
    curl = bin_dir / "curl"
    curl.write_text(
        r'''#!/bin/bash
printf '%s\n' "$*" >>"$CURL_LOG"

config=""
output=""
previous=""
for argument in "$@"; do
  if [[ "$previous" == "config" ]]; then
    config="$argument"
    previous=""
    continue
  fi
  if [[ "$previous" == "output" ]]; then
    output="$argument"
    previous=""
    continue
  fi
  case "$argument" in
    --config) previous="config" ;;
    --output) previous="output" ;;
  esac
done

if [[ -n "$config" ]]; then
  /bin/cp "$config" "$CONFIG_CAPTURE"
  python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' \
    "$config" >"$CONFIG_MODE_CAPTURE"
fi

case " $* " in
  *"/workers/scripts/"*)
    if [[ "${FAIL_UPLOAD:-}" == "1" ]]; then
      printf '%s\n' '{"success":false,"errors":[{"message":"upload rejected"}]}'
    else
      printf '%s\n' '{"success":true,"result":{}}'
    fi
    ;;
  *"/workers/domains"*)
    printf '%s\n' '{"success":true,"result":{}}'
    ;;
  *"/zones?name="*)
    printf '%s\n' '{"success":true,"result":[{"id":"zone-from-api"}]}'
    ;;
  *"/accounts "*)
    printf '%s\n' '{"success":true,"result":[{"id":"account-from-api"}]}'
    ;;
  *"https://logpile.ai/?logpile-deploy-verify="*)
    if [[ "${REMOTE_MISMATCH:-}" == "1" ]]; then
      printf '%s' 'stale deployment' >"$output"
    else
      /bin/cp "$DEPLOY_HTML" "$output"
    fi
    ;;
  *)
    printf '%s\n' '{"success":false,"errors":[{"message":"unexpected request"}]}'
    ;;
esac
'''
    )
    curl.chmod(0o755)


def _run_deploy(
    tmp_path: Path, *, fail_upload: bool = False, remote_mismatch: bool = False
) -> tuple[subprocess.CompletedProcess[str], str, Path, Path]:
    checkout, html = _deploy_checkout(tmp_path)
    bin_dir = _deploy_path(tmp_path)
    _write_fake_curl(bin_dir)
    curl_log = tmp_path / "curl.log"
    config_capture = tmp_path / "curl-config.capture"
    config_mode_capture = tmp_path / "curl-config-mode.capture"
    env = {
        "PATH": str(bin_dir),
        "CLOUDFLARE_EMAIL": "deploy@example.com",
        "CLOUDFLARE_GLOBAL_API_KEY": "super-secret-global-key",
        "CLOUDFLARE_ACCOUNT_ID": "explicit-account",
        "CLOUDFLARE_ZONE_ID": "explicit-zone",
        "CURL_LOG": str(curl_log),
        "CONFIG_CAPTURE": str(config_capture),
        "CONFIG_MODE_CAPTURE": str(config_mode_capture),
        "DEPLOY_HTML": str(html),
        "FAIL_UPLOAD": "1" if fail_upload else "0",
        "REMOTE_MISMATCH": "1" if remote_mismatch else "0",
    }
    result = subprocess.run(
        ["/bin/bash", str(checkout / "scripts" / "deploy_landing.sh")],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    return result, curl_log.read_text(), config_capture, config_mode_capture


def test_deploy_landing_hardens_credentials_and_verifies_hash(tmp_path: Path) -> None:
    result, curl_log, config_capture, config_mode_capture = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "worker upload: ok" in result.stdout
    assert "domain attach: ok" in result.stdout
    assert "verified https://logpile.ai (sha256" in result.stdout
    assert "deployed https://logpile.ai" in result.stdout
    assert "--fail-with-body" in curl_log
    assert "explicit-account" in curl_log
    assert not any(line.endswith("/accounts") for line in curl_log.splitlines())
    assert "super-secret-global-key" not in curl_log
    assert "deploy@example.com" not in curl_log
    assert config_mode_capture.read_text().strip() == "0o600"
    config = config_capture.read_text()
    assert "X-Auth-Email: deploy@example.com" in config
    assert "X-Auth-Key: super-secret-global-key" in config

    temporary_paths = [
        *re.findall(r"--config ([^ ]+)", curl_log),
        *re.findall(r"worker\.js=@([^; ]+)", curl_log),
        *re.findall(r"--output ([^ ]+)", curl_log),
    ]
    assert temporary_paths
    assert all(not Path(path).exists() for path in temporary_paths)


def test_deploy_landing_exits_nonzero_on_cloudflare_failure(tmp_path: Path) -> None:
    result, curl_log, _, _ = _run_deploy(tmp_path, fail_upload=True)

    assert result.returncode != 0
    assert "worker upload: Cloudflare API failed" in result.stderr
    assert "deployed https://logpile.ai" not in result.stdout
    assert "/workers/domains" not in curl_log
    assert "logpile-deploy-verify" not in curl_log


def test_deploy_landing_exits_nonzero_on_hash_mismatch(tmp_path: Path) -> None:
    result, curl_log, _, _ = _run_deploy(tmp_path, remote_mismatch=True)

    assert result.returncode != 0
    assert "deployment verification failed" in result.stderr
    assert "deployed https://logpile.ai" not in result.stdout
    assert sum(
        "https://logpile.ai/?logpile-deploy-verify=" in line
        for line in curl_log.splitlines()
    ) == 3


def test_landing_distinct_repo_metric_is_labeled_repos() -> None:
    builder = (ROOT / "scripts" / "build_landing.py").read_text()
    landing = (ROOT / "site" / "index.html").read_text()

    assert "COUNT(DISTINCT repo_name) AS repos" in builder
    assert "repo checkouts" not in builder
    assert "repo checkouts" not in landing
    assert " repos — the author's index" in landing


def test_readme_quickstart_uses_checkout_wrapper() -> None:
    readme = (ROOT / "README.md").read_text()
    quickstart = readme.split("## Quick start", 1)[1].split("\n---", 1)[0]

    assert "./logpile.sh sync" in quickstart
    assert "./logpile.sh search" in quickstart
    assert "./logpile.sh serve" in quickstart
    assert "uv venv" not in quickstart


def test_readme_rejects_shared_network_sqlite_and_live_db_git_backup() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "/Volumes/team-share" not in readme
    assert "must not open one `logpile.db` over NFS, SMB" in readme
    assert "./logpile.sh db-backup" in readme
    assert "do not commit the live database" in readme
