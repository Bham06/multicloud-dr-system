#!/bin/bash

# Monitor replication lag
psql -h $RDS_ENDPOINT -U appuser -d application -c "
SELECT 
  subname,
  received_lsn,
  latest_end_lsn,
  pg_size_pretty(pg_wal_lsn_diff(received_lsn, latest_end_lsn)) as lag
FROM pg_stat_subscription;
"