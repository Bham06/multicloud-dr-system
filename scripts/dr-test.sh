#!/bin/bash
# Automated DR Testing Framework

set -e

PROJECT_ID="final-year-project1-484523"
LB_IP="34.36.128.254"
AWS_EIP="98.90.182.178"

echo "=== DR System Test Suite ==="
echo "Started: $(date)"

# Test 1: Health Check Endpoints
echo ""
echo "Test 1: Health Check Endpoints"
GCP_HEALTH=$(curl -s http://$LB_IP/health | jq -r '.status')
AWS_HEALTH=$(curl -s http://$AWS_EIP/health | jq -r '.status')

if [ "$GCP_HEALTH" == "healthy" ]; then
  echo "✓ GCP backend healthy"
else
  echo "✗ GCP backend unhealthy"
  exit 1
fi

if [ "$AWS_HEALTH" == "healthy" ]; then
  echo "✓ AWS backend healthy"
else
  echo "✗ AWS backend unhealthy"
  exit 1
fi

# Test 2: Auto-Failover Function
echo ""
echo "Test 2: Auto-Failover Function"
FUNCTION_RESULT=$(gcloud functions call auto-failover-function \
  --gen2 \
  --region=us-east1 \
  --format=json | jq -r '.result.status')

if [ "$FUNCTION_RESULT" == "success" ]; then
  echo "✓ Auto-failover function executing"
else
  echo "✗ Auto-failover function failed"
  exit 1
fi

# Test 3: Firestore State
echo ""
echo "Test 3: Firestore State"
STATE=$(gcloud firestore documents describe current_state \
  --collection-path=failover_state \
  --format=json | jq -r '.fields.active_backend.stringValue')

echo "Current active backend: $STATE"
if [ "$STATE" == "gcp" ] || [ "$STATE" == "aws" ]; then
  echo "✓ Valid state in Firestore"
else
  echo "✗ Invalid state in Firestore"
  exit 1
fi

# Test 4: Database Replication
echo ""
echo "Test 4: Database Replication"
# Insert test row on Cloud SQL
TEST_ID=$(date +%s)
gcloud sql connect dr-primary-db --user=postgres --database=application << EOF
INSERT INTO test_table (id, data) VALUES ($TEST_ID, 'dr-test-$(date)');
EOF

# Wait for replication
sleep 10

# Check if row exists on RDS
RDS_COUNT=$(ssh ubuntu@$AWS_EIP "psql -h RDS_ENDPOINT -U appuser -d application -t -c \"SELECT COUNT(*) FROM test_table WHERE id=$TEST_ID;\"")

if [ "$RDS_COUNT" -eq 1 ]; then
  echo "✓ Database replication working"
else
  echo "✗ Database replication failed"
  exit 1
fi

# Test 5: VPN Tunnel
# echo ""
# echo "Test 5: VPN Tunnel Status"
# TUNNEL_STATUS=$(gcloud compute vpn-tunnels describe tunnel-to-aws \
#   --region=us-east1 \
#   --format="get(status)")

# if [ "$TUNNEL_STATUS" == "ESTABLISHED" ]; then
#   echo "✓ VPN tunnel established"
# else
#   echo "⚠ VPN tunnel not established (may not be using VPN)"
# fi

echo ""
echo "=== All Tests Passed ==="
echo "Completed: $(date)"

