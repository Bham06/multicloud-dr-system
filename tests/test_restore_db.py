"""
Behavioural tests for the database restore script.

The restore path is the whole point of the system, and its failure mode is the
dangerous kind: a restore that silently does nothing while reporting success
leaves you believing you have a recoverable standby right up until you need it.

These run the real script against stub psql/aws/pg_isready binaries placed on
PATH, so they exercise the actual control flow rather than a reimplementation.
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTORE_SCRIPT = REPO_ROOT / "terraform" / "aws" / "scripts" / "restore-db.sh"

BACKUP_KEY = "backups/application_20260811_120000.sql"
LS_LINE = f"2026-08-11 12:00:00        512 {BACKUP_KEY}"


def write_stub(path, body):
    path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def harness(tmp_path):
    """A sandbox with stub CLIs on PATH and the script's config pointed at it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    temp_dir = tmp_path / "work"
    temp_dir.mkdir()

    psql_log = tmp_path / "psql.args"

    # Distinguishes the restore invocation (--file=) from the verification
    # query, so each can be made to fail independently.
    write_stub(
        bin_dir / "psql",
        f"""
        printf '%s\\n' "$*" >> "{psql_log}"
        for arg in "$@"; do
            case "$arg" in
                --file=*) exit "${{STUB_RESTORE_EXIT:-0}}" ;;
            esac
        done
        echo "${{STUB_TABLE_COUNT:-7}}"
        exit "${{STUB_VERIFY_EXIT:-0}}"
        """,
    )

    write_stub(
        bin_dir / "aws",
        """
        if [ "$1" = "secretsmanager" ]; then
            [ "${STUB_SECRET_EXIT:-0}" != "0" ] && exit "$STUB_SECRET_EXIT"
            echo "${STUB_SECRET_VALUE:-fetched-password}"
            exit 0
        fi
        if [ "$1" = "s3" ] && [ "$2" = "ls" ]; then
            [ -n "${STUB_LS_OUTPUT:-}" ] && echo "$STUB_LS_OUTPUT"
            exit 0
        fi
        if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then
            [ "${STUB_CP_EXIT:-0}" != "0" ] && exit "$STUB_CP_EXIT"
            # Note the '-' rather than ':-': an empty STUB_DUMP_CONTENT is a
            # deliberate test case, not an absent one.
            printf '%s' "${STUB_DUMP_CONTENT-CREATE TABLE t (i int);}" > "$4"
            exit 0
        fi
        exit 1
        """,
    )

    write_stub(bin_dir / "pg_isready", 'exit "${STUB_ISREADY_EXIT:-0}"')
    write_stub(bin_dir / "logger", "exit 0")
    # flock is absent on macOS; the tests are single-threaded so a no-op is fine.
    write_stub(bin_dir / "flock", "exit 0")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(
        S3_BUCKET_NAME="dr-storage-secondary-test",
        RDS_HOST="rds.test.local",
        RDS_PORT="5432",
        RDS_DB="application",
        RDS_USER="appuser",
        RDS_PASSWORD="hunter2",
        TEMP_DIR=str(temp_dir),
        LOG_FILE=str(tmp_path / "restore.log"),
        LOCK_FILE=str(tmp_path / "restore.lock"),
        STUB_LS_OUTPUT=LS_LINE,
    )

    class Harness:
        def __init__(self):
            self.env = env
            self.temp_dir = temp_dir
            self.marker = temp_dir / ".last_restored"
            self.psql_log = psql_log

        def run(self, **overrides):
            self.env.update({k: str(v) for k, v in overrides.items()})
            return subprocess.run(
                ["bash", str(RESTORE_SCRIPT)],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=60,
            )

        def psql_calls(self):
            if not self.psql_log.exists():
                return []
            return [c for c in self.psql_log.read_text().splitlines() if c.strip()]

    return Harness()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestRestore:
    # ----------------------------------------------------------------------
    # The failure that matters
    # ----------------------------------------------------------------------

    def test_failed_restore_exits_nonzero(self, harness):
        """
        Regression test. The previous script piped psql through tee, so the
        `if` tested tee's exit status and every restore looked successful -
        including one that never connected.
        """
        result = harness.run(STUB_RESTORE_EXIT=1)
        assert result.returncode != 0, result.stdout

    def test_failed_restore_does_not_record_success(self, harness):
        """
        The marker is what stops the next run retrying. Writing it after a
        failed restore converts a transient error into permanent data loss.
        """
        harness.run(STUB_RESTORE_EXIT=1)
        assert not harness.marker.exists()

    def test_failed_restore_is_retried_on_the_next_run(self, harness):
        harness.run(STUB_RESTORE_EXIT=1)
        result = harness.run(STUB_RESTORE_EXIT=0)
        assert result.returncode == 0, result.stdout
        assert harness.marker.read_text().strip() == BACKUP_KEY

    # ----------------------------------------------------------------------
    # Verification
    # ----------------------------------------------------------------------

    def test_restore_with_no_tables_is_treated_as_failure(self, harness):
        """A dump that applies cleanly but leaves an empty database is not a
        recovery."""
        result = harness.run(STUB_RESTORE_EXIT=0, STUB_TABLE_COUNT=0)
        assert result.returncode != 0
        assert not harness.marker.exists()

    def test_unverifiable_restore_is_treated_as_failure(self, harness):
        result = harness.run(STUB_RESTORE_EXIT=0, STUB_VERIFY_EXIT=1)
        assert result.returncode != 0
        assert not harness.marker.exists()

    # ----------------------------------------------------------------------
    # Restore invocation
    # ----------------------------------------------------------------------

    def test_restore_is_atomic_and_stops_on_error(self, harness):
        """
        Without ON_ERROR_STOP psql reports success after every statement has
        failed. Without --single-transaction a mid-dump failure leaves the
        standby with the schema dropped and nothing to serve, because the dump
        drops existing objects before recreating them.
        """
        harness.run()
        restore_call = next(c for c in harness.psql_calls() if "--file=" in c)
        assert "ON_ERROR_STOP=1" in restore_call
        assert "--single-transaction" in restore_call

    def test_happy_path_records_the_restored_key(self, harness):
        result = harness.run()
        assert result.returncode == 0, result.stdout
        assert harness.marker.read_text().strip() == BACKUP_KEY

    # ----------------------------------------------------------------------
    # Refusing to act
    # ----------------------------------------------------------------------

    def test_unreachable_database_does_not_attempt_a_restore(self, harness):
        result = harness.run(STUB_ISREADY_EXIT=1)
        assert result.returncode != 0
        assert not any("--file=" in c for c in harness.psql_calls())

    def test_no_backups_is_not_an_error(self, harness):
        result = harness.run(STUB_LS_OUTPUT="")
        assert result.returncode == 0, result.stdout
        assert harness.psql_calls() == []

    def test_already_restored_backup_is_skipped(self, harness):
        harness.run()
        before = len(harness.psql_calls())

        result = harness.run()

        assert result.returncode == 0
        assert len(harness.psql_calls()) == before, "re-restored an applied backup"

    # ----------------------------------------------------------------------
    # Credentials
    # ----------------------------------------------------------------------

    def test_password_is_fetched_from_secrets_manager_when_absent(self, harness):
        """The password must not have to exist on disk for the cron to work."""
        del harness.env["RDS_PASSWORD"]
        result = harness.run(DB_SECRET_ARN="arn:aws:secretsmanager:::secret:dr/db-password")
        assert result.returncode == 0, result.stdout
        assert harness.marker.read_text().strip() == BACKUP_KEY

    def test_missing_credentials_fail_before_touching_the_database(self, harness):
        del harness.env["RDS_PASSWORD"]
        result = harness.run()
        assert result.returncode != 0
        assert not any("--file=" in c for c in harness.psql_calls())

    def test_unretrievable_secret_is_not_treated_as_an_empty_password(self, harness):
        del harness.env["RDS_PASSWORD"]
        result = harness.run(
            DB_SECRET_ARN="arn:aws:secretsmanager:::secret:dr/db-password",
            STUB_SECRET_EXIT=1,
        )
        assert result.returncode != 0
        assert not any("--file=" in c for c in harness.psql_calls())

    def test_failed_download_does_not_attempt_a_restore(self, harness):
        result = harness.run(STUB_CP_EXIT=1)
        assert result.returncode != 0
        assert not any("--file=" in c for c in harness.psql_calls())

    def test_empty_dump_does_not_attempt_a_restore(self, harness):
        """An empty object would drop nothing and create nothing, but would
        still mark the backup as applied."""
        result = harness.run(STUB_DUMP_CONTENT="")
        assert result.returncode != 0
        assert not any("--file=" in c for c in harness.psql_calls())
