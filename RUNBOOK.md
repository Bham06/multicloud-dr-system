# DR System Operational Runbook

## Table of Contents
1. [Normal Operations](#normal-operations)
2. [Manual Failover Procedure](#manual-failover)
3. [Manual Failback Procedure](#manual-failback)
4. [Troubleshooting](#troubleshooting)
5. [Disaster Recovery Testing](#dr-testing)
6. [Emergency Contacts](#contacts)

---

## Normal Operations

### Daily Health Check
```bash
# Check system status
gcloud functions call auto-failover-function --gen2

# View dashboard
echo "https://console.cloud.google.com/monitoring/dashboards"

# Check replication lag
ssh ubuntu@AWS_EC2_IP "psql -h RDS_ENDPOINT -U appuser -d application -c \"SELECT * FROM pg_stat_subscription;\""
```

### Weekly Tasks
- Review monitoring dashboard for anomalies
- Check VPN tunnel uptime
- Verify database replication lag < 10 seconds
- Review alert history

### Monthly Tasks
- Perform DR drill (see DR Testing section)
- Review and update runbooks
- Rotate VPN shared secret
- Review SLO compliance

---

## Manual Failover Procedure

**When to use:** Auto-failover hasn't triggered but GCP is experiencing issues

### Pre-Failover Checklist
- [ ] Confirm GCP backend is actually unhealthy
- [ ] Verify AWS backend is healthy
- [ ] Notify team of planned failover
- [ ] Have rollback plan ready

### Failover Steps
```bash
# 1. Verify current active backend
gcloud compute url-maps describe dr-url-map \
  --global \
  --format="get(defaultService)"

# Should show: .../dr-backend-gcp-primary

# 2. Check AWS backend health
curl http://AWS_EIP/health

# Should return: {"status":"healthy","provider":"AWS"}

# 3. Update URL map to AWS
gcloud compute url-maps set-default-service dr-url-map \
  --default-service=dr-backend-aws-secondary \
  --global

# 4. Verify failover
curl http://LOAD_BALANCER_IP/
# Should now show AWS provider

# 5. Update Firestore state manually
cat > /tmp/update_state.py << 'EOF'
from google.cloud import firestore
db = firestore.Client()
doc_ref = db.collection('failover_state').document('current_state')
doc_ref.set({
    'active_backend': 'aws',
    'last_change': firestore.SERVER_TIMESTAMP,
    'consecutive_failures': 0,
    'manual_failover': True
}, merge=True)
print("State updated to AWS")
EOF

python3 /tmp/update_state.py

# 6. Monitor for 10 minutes
watch -n 10 'curl -s http://LOAD_BALANCER_IP/health | jq'
```

**Estimated RTO:** 2-3 minutes

---

## Manual Failback Procedure

**When to use:** GCP has recovered and you want to return to primary

### Pre-Failback Checklist
- [ ] Confirm GCP backend is healthy for 15+ minutes
- [ ] Verify database replication is caught up
- [ ] Check no active user sessions (or notify users)
- [ ] Have rollback plan ready

### Failback Steps
```bash
# 1. Verify GCP health
gcloud compute instances describe dr-app-primary \
  --zone=us-east1-b \
  --format="get(status)"

# Should show: RUNNING

curl http://GCP_INTERNAL_IP/health
# Should return: {"status":"healthy"}

# 2. Check replication lag
ssh ubuntu@AWS_EIP << 'EOF'
psql -h RDS_ENDPOINT -U appuser -d application -c "
SELECT 
  pg_wal_lsn_diff(received_lsn, latest_end_lsn) as lag_bytes,
  last_msg_receipt_time
FROM pg_stat_subscription;
"
EOF

# lag_bytes should be 0 or very small

# 3. Update URL map to GCP
gcloud compute url-maps set-default-service dr-url-map \
  --default-service=dr-backend-gcp-primary \
  --global

# 4. Verify failback
curl http://LOAD_BALANCER_IP/
# Should now show GCP provider

# 5. Update Firestore state
python3 << 'EOF'
from google.cloud import firestore
db = firestore.Client()
doc_ref = db.collection('failover_state').document('current_state')
doc_ref.set({
    'active_backend': 'gcp',
    'last_change': firestore.SERVER_TIMESTAMP,
    'consecutive_failures': 0,
    'manual_failback': True
}, merge=True)
print("State updated to GCP")
EOF

# 6. Monitor for 10 minutes
```

**Estimated RTO:** 2-3 minutes

---

## Troubleshooting

### Issue: Auto-Failover Not Triggering

**Symptoms:** GCP is down but system hasn't failed over

**Diagnosis:**
```bash
# Check function is running
gcloud scheduler jobs describe auto-failover-scheduler \
  --location=us-east1

# Check recent executions
gcloud functions logs read auto-failover-function \
  --gen2 \
  --limit=20

# Check current state
gcloud firestore documents describe current_state \
  --collection-path=failover_state
```

**Resolution:**
- Check `consecutive_failures` counter - might need 3 failures
- Verify function has correct IAM permissions
- Check if scheduler is paused
- Perform manual failover if urgent

### Issue: High Replication Lag

**Symptoms:** `pg_stat_subscription` shows large `lag_bytes`

**Diagnosis:**
```bash
# Check VPN tunnel
gcloud compute vpn-tunnels describe tunnel-to-aws \
  --region=us-east1

# Check Cloud SQL performance
gcloud monitoring time-series list \
  --filter='metric.type="cloudsql.googleapis.com/database/cpu/utilization"'

# Check RDS performance
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name CPUUtilization \
  --dimensions Name=DBInstanceIdentifier,Value=dr-secondary-db \
  --statistics Average \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300
```

**Resolution:**
- If VPN down: Restart tunnel or recreate
- If Cloud SQL overloaded: Scale up instance
- If RDS overloaded: Scale up instance
- If network congestion: Check VPN bandwidth

### Issue: Both Backends Unhealthy

**Symptoms:** All health checks failing

**Immediate Actions:**
1. Check if this is a monitoring issue (can you reach the sites manually?)
2. Check load balancer configuration
3. Check firewall rules
4. Check if Cloud Functions VPC connector is working
5. Emergency: Deploy static maintenance page

**Resolution:** See Emergency Response below

---

## DR Testing Procedures

### Monthly DR Drill (Planned Outage)

**Objective:** Validate RTO/RPO targets

**Schedule:** Last Sunday of each month, 2:00 AM UTC

**Procedure:**
```bash
#!/bin/bash
# dr-drill.sh - Automated DR testing

echo "=== DR Drill Starting at $(date) ==="

# 1. Record start time
START_TIME=$(date +%s)

# 2. Stop GCP backend (simulate failure)
echo "Simulating GCP failure..."
gcloud compute instances stop dr-app-primary --zone=us-east1-b

# 3. Wait for auto-failover (max 10 minutes)
echo "Waiting for auto-failover..."
for i in {1..60}; do
  BACKEND=$(gcloud compute url-maps describe dr-url-map --global --format="get(defaultService)")
  if [[ "$BACKEND" == *"aws"* ]]; then
    FAILOVER_TIME=$(date +%s)
    RTO=$((FAILOVER_TIME - START_TIME))
    echo "✓ Failover completed in $RTO seconds"
    break
  fi
  sleep 10
done

# 4. Verify AWS is serving traffic
echo "Verifying AWS backend..."
RESPONSE=$(curl -s http://LOAD_BALANCER_IP/)
if [[ "$RESPONSE" == *"AWS"* ]]; then
  echo "✓ AWS backend serving traffic"
else
  echo "✗ AWS backend NOT serving traffic - DRILL FAILED"
  exit 1
fi

# 5. Check data integrity
echo "Checking database replication..."
ssh ubuntu@AWS_EIP << 'EOSSH'
psql -h RDS_ENDPOINT -U appuser -d application -c "SELECT COUNT(*) FROM your_table;"
EOSSH

# 6. Restore GCP (test failback)
echo "Starting GCP failback..."
gcloud compute instances start dr-app-primary --zone=us-east1-b

# Wait for GCP to be healthy
sleep 120

# 7. Measure failback time
FAILBACK_START=$(date +%s)
for i in {1..60}; do
  BACKEND=$(gcloud compute url-maps describe dr-url-map --global --format="get(defaultService)")
  if [[ "$BACKEND" == *"gcp"* ]]; then
    FAILBACK_TIME=$(date +%s)
    RTO_FAILBACK=$((FAILBACK_TIME - FAILBACK_START))
    echo "✓ Failback completed in $RTO_FAILBACK seconds"
    break
  fi
  sleep 10
done

# 8. Report results
echo "=== DR Drill Complete ==="
echo "Failover RTO: $RTO seconds"
echo "Failback RTO: $RTO_FAILBACK seconds"
echo "Target RTO: 60 seconds"

if [ $RTO -le 60 ]; then
  echo "✓ RTO target MET"
else
  echo "✗ RTO target MISSED by $((RTO - 60)) seconds"
fi
```

**Success Criteria:**
- RTO < 60 seconds
- RPO < 10 seconds
- No data loss
- All monitoring alerts fired correctly

---

## Emergency Contacts

**GCP Support:** https://console.cloud.google.com/support
**AWS Support:** https://console.aws.amazon.com/support

---

*Last Updated: 17th March 2026*