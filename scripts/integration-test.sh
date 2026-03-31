#!/bin/bash
# Integration Test - Verifies end-to-end data flow

set -e

TEST_ID=$(date +%s)
TEST_DATA="integration-test-$(date +%Y%m%d-%H%M%S)"

echo "=== Integration Test Suite ==="
echo "Test ID: $TEST_ID"
echo ""

# Test 1: Write to GCP Cloud SQL
echo "Test 1: Writing to Cloud SQL..."
gcloud sql connect dr-primary-db --user=postgres --database=application << EOF
INSERT INTO test_table (id, data, created_at) 
VALUES ($TEST_ID, '$TEST_DATA', NOW());
EOF

echo "✓ Data written to Cloud SQL"

# Test 2: Verify replication to RDS (wait for lag)
echo ""
echo "Test 2: Verifying replication to RDS..."
sleep 15  # Wait for replication

RDS_DATA=$(ssh ubuntu@AWS_EIP "psql -h RDS_ENDPOINT -U appuser -d application -t -c \"SELECT data FROM test_table WHERE id=$TEST_ID;\"" | xargs)

if [ "$RDS_DATA" == "$TEST_DATA" ]; then
  echo "✓ Data replicated to RDS correctly"
else
  echo "✗ Replication failed or delayed"
  echo "  Expected: $TEST_DATA"
  echo "  Got: $RDS_DATA"
  exit 1
fi

# Test 3: Check replication lag
echo ""
echo "Test 3: Checking replication lag..."
LAG=$(ssh ubuntu@AWS_EIP "psql -h RDS_ENDPOINT -U appuser -d application -t -c \"SELECT pg_wal_lsn_diff(received_lsn, latest_end_lsn) FROM pg_stat_subscription;\"" | xargs)

echo "Current lag: $LAG bytes"
if [ $LAG -lt 1000000 ]; then  # Less than 1MB
  echo "✓ Replication lag acceptable (<1MB)"
else
  echo "⚠️ WARNING: High replication lag (${LAG} bytes)"
fi

# Test 4: VPN connectivity
echo ""
echo "Test 4: Testing VPN connectivity..."
PING_RESULT=$(ssh ubuntu@AWS_EIP "ping -c 3 CLOUDSQL_PRIVATE_IP" | grep "3 received")

if [ -n "$PING_RESULT" ]; then
  echo "✓ VPN connectivity working"
else
  echo "⚠️ WARNING: VPN connectivity issue"
fi

# Cleanup
echo ""
echo "Cleaning up test data..."
gcloud sql connect dr-primary-db --user=postgres --database=application << EOF
DELETE FROM test_table WHERE id=$TEST_ID;
EOF

echo ""
echo "✅ Integration tests passed"