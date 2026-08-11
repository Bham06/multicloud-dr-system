import os
import time
from datetime import datetime, timezone

import googleapiclient.discovery
from google.cloud import logging as cloud_logging

# Environment variables
PROJECT_ID = os.environ.get('GCP_PROJECT')
DB_CONNECTION_NAME = os.environ.get('DB_CONNECTION_NAME')  # project:region:instance
DB_NAME = os.environ.get('DB_NAME')
GCS_BACKUP_BUCKET = os.environ.get('GCS_BACKUP_BUCKET')

# Initialize clients
logging_client = cloud_logging.Client()
logger = logging_client.logger('db-backup')

# restore-db.sh on the AWS side polls this prefix and restores the
# lexically-last object, so the timestamp must sort chronologically.
BACKUP_PREFIX = 'backups/'
TIMESTAMP_FORMAT = '%Y%m%d_%H%M%S'

# The Cloud Function timeout is 540s; stop polling before it kills us so the
# failure is logged rather than appearing as an opaque timeout.
POLL_INTERVAL_SEC = 10
POLL_TIMEOUT_SEC = 480


def instance_id(connection_name):
    """Cloud SQL connection names are project:region:instance."""
    return connection_name.split(':')[-1]


def backup_object_name():
    timestamp = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
    return f"{BACKUP_PREFIX}{DB_NAME}_{timestamp}.sql"


def wait_for_operation(service, operation_name):
    """
    Poll a Cloud SQL operation until it reaches DONE.

    Returns (succeeded, message). The export runs asynchronously on the Cloud
    SQL service, so returning before it finishes would report success for a
    backup that may not exist.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SEC

    while time.monotonic() < deadline:
        operation = service.operations().get(
            project=PROJECT_ID,
            operation=operation_name
        ).execute()

        if operation.get('status') == 'DONE':
            error = operation.get('error')
            if error:
                # errors.errors is a list of {kind, code, message}
                details = '; '.join(
                    item.get('message', 'unknown')
                    for item in error.get('errors', [])
                )
                return False, details or 'export failed without detail'
            return True, 'export completed'

        time.sleep(POLL_INTERVAL_SEC)

    return False, f"export did not complete within {POLL_TIMEOUT_SEC}s"


def backup_database(request):
    """
    Export the Cloud SQL database to GCS as plain SQL.

    Triggered by Cloud Scheduler over HTTP. The object landing in the backup
    bucket fires an object-finalize event that the gcs-s3-sync function picks
    up, which is what gets the backup across to S3.
    """

    logger.log_text("=== Database Backup Started ===", severity='INFO')

    missing = [
        name for name, value in (
            ('GCP_PROJECT', PROJECT_ID),
            ('DB_CONNECTION_NAME', DB_CONNECTION_NAME),
            ('DB_NAME', DB_NAME),
            ('GCS_BACKUP_BUCKET', GCS_BACKUP_BUCKET),
        ) if not value
    ]
    if missing:
        message = f"Missing required environment variables: {', '.join(missing)}"
        logger.log_text(f"✗ {message}", severity='ERROR')
        return {'status': 'error', 'message': message}, 500

    object_name = backup_object_name()
    destination_uri = f"gs://{GCS_BACKUP_BUCKET}/{object_name}"
    instance = instance_id(DB_CONNECTION_NAME)

    service = googleapiclient.discovery.build('sqladmin', 'v1beta4')

    # clean/ifExists emit DROP ... IF EXISTS ahead of each CREATE so the dump
    # can be replayed onto an RDS instance that already holds an older restore.
    body = {
        'exportContext': {
            'kind': 'sql#exportContext',
            'fileType': 'SQL',
            'uri': destination_uri,
            'databases': [DB_NAME],
            'sqlExportOptions': {
                'clean': True,
                'ifExists': True,
            },
        }
    }

    try:
        operation = service.instances().export(
            project=PROJECT_ID,
            instance=instance,
            body=body
        ).execute()
    except Exception as e:
        logger.log_text(
            f"✗ Failed to start export of {instance} to {destination_uri}: {str(e)}",
            severity='ERROR'
        )
        return {'status': 'error', 'message': str(e)}, 500

    logger.log_text(
        f"Export started for instance={instance} -> {destination_uri} "
        f"(operation={operation.get('name')})",
        severity='INFO'
    )

    succeeded, message = wait_for_operation(service, operation.get('name'))

    if not succeeded:
        logger.log_text(
            f"✗ Backup failed for {destination_uri}: {message}",
            severity='ERROR'
        )
        return {'status': 'error', 'message': message}, 500

    logger.log_text(
        f"✓ Backup completed: {destination_uri}",
        severity='INFO'
    )

    return {
        'status': 'success',
        'backup_uri': destination_uri,
        'object_name': object_name,
        'instance': instance,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
