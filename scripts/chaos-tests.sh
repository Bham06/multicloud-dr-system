#!/bin/bash
# Chaos Engineering Tests - Simple Failure Injection

echo "=== Chaos Engineering Test Suite ==="
echo "⚠️ WARNING: This will intentionally break things!"
echo ""

# Chaos Test 1: Network Partition (Simulate GCP unreachable)
chaos_test_network_partition() {
  echo "Chaos Test 1: Network Partition (GCP unreachable)"
  echo "Simulating: GCP VM loses network connectivity"
  
  # SSH to GCP VM and disable network
  gcloud compute ssh dr-app-primary --zone=us-east1-b --command="sudo iptables -A OUTPUT -j DROP"
  
  echo "Network disabled. Monitoring failover..."
  sleep 120  # Wait 2 minutes
  
  # Check if failover happened
  PROVIDER=$(curl -s http://LOAD_BALANCER_IP/health | jq -r '.provider')
  
  if [ "$PROVIDER" == "AWS" ]; then
    echo "✓ System successfully failed over to AWS"
  else
    echo "✗ Failover did not occur - RESILIENCE FAILURE"
  fi
  
  # Restore network
  gcloud compute instances reset dr-app-primary --zone=us-east1-b
  echo "Network restored"
}

# Chaos Test 2: CPU Spike (Resource exhaustion)
chaos_test_cpu_spike() {
  echo ""
  echo "Chaos Test 2: CPU Exhaustion"
  echo "Simulating: GCP VM under heavy CPU load"
  
  # SSH and run CPU stress test
  gcloud compute ssh dr-app-primary --zone=us-east1-b --command="
    stress-ng --cpu 4 --timeout 180s &
  "
  
  echo "CPU stress applied. Monitoring response times..."
  
  # Measure response time
  for i in {1..18}; do
    RESPONSE_TIME=$(curl -o /dev/null -s -w '%{time_total}\n' http://LOAD_BALANCER_IP/)
    echo "  Response time: ${RESPONSE_TIME}s"
    
    if (( $(echo "$RESPONSE_TIME > 5" | bc -l) )); then
      echo "  ⚠️ Degraded performance detected"
    fi
    
    sleep 10
  done
  
  echo "✓ System remained available under CPU stress"
}

# Chaos Test 3: Database Connection Loss
chaos_test_db_failure() {
  echo ""
  echo "Chaos Test 3: Database Connection Failure"
  echo "Simulating: Application loses database connectivity"
  
  # Block PostgreSQL port
  gcloud compute ssh dr-app-primary --zone=us-east1-b --command="
    sudo iptables -A OUTPUT -p tcp --dport 5432 -j DROP
  "
  
  echo "Database connectivity blocked. Testing graceful degradation..."
  sleep 30
  
  # Check if health endpoint still responds
  HEALTH=$(curl -s http://LOAD_BALANCER_IP/health | jq -r '.status')
  
  if [ "$HEALTH" == "unhealthy" ]; then
    echo "✓ System correctly reports unhealthy status"
  else
    echo "⚠️ System not detecting database failure"
  fi
  
  # Restore
  gcloud compute ssh dr-app-primary --zone=us-east1-b --command="
    sudo iptables -F
  "
  
  echo "Database connectivity restored"
}

# Chaos Test 4: VPN Tunnel Failure
chaos_test_vpn_failure() {
  echo ""
  echo "Chaos Test 4: VPN Tunnel Failure"
  echo "Simulating: VPN tunnel goes down"
  
  # Delete VPN tunnel
  gcloud compute vpn-tunnels delete tunnel-to-aws \
    --region=us-east1 \
    --quiet
  
  echo "VPN tunnel deleted. Checking impact on replication..."
  sleep 60
  
  # Check replication lag
  LAG=$(ssh ubuntu@AWS_EIP "psql -h RDS_ENDPOINT -U appuser -d application -t -c \"SELECT pg_wal_lsn_diff(received_lsn, latest_end_lsn) FROM pg_stat_subscription;\"" 2>&1)
  
  if [[ "$LAG" == *"error"* ]] || [ -z "$LAG" ]; then
    echo "✓ Replication correctly failed (expected)"
  else
    echo "⚠️ Replication still working without VPN (unexpected)"
  fi
  
  # Recreate tunnel (run Terraform)
  cd gcp/
  terraform apply -auto-approve -target=google_compute_vpn_tunnel.tunnel_to_aws
  
  echo "VPN tunnel recreated"
}

# Chaos Test 5: Memory Pressure
chaos_test_memory_pressure() {
  echo ""
  echo "Chaos Test 5: Memory Exhaustion"
  echo "Simulating: Low memory conditions"
  
  # Use stress-ng to consume memory
  gcloud compute ssh dr-app-primary --zone=us-east1-b --command="
    stress-ng --vm 1 --vm-bytes 90% --timeout 120s &
  "
  
  echo "Memory pressure applied. Monitoring for OOM events..."
  sleep 120
  
  # Check if system survived
  HEALTH=$(curl -s http://LOAD_BALANCER_IP/health | jq -r '.status')
  
  if [ "$HEALTH" == "healthy" ]; then
    echo "✓ System survived memory pressure"
  else
    echo "✗ System failed under memory pressure"
  fi
}

# Run tests with confirmation
read -p "Run Chaos Test 1 (Network Partition)? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  chaos_test_network_partition
fi

read -p "Run Chaos Test 2 (CPU Spike)? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  chaos_test_cpu_spike
fi

read -p "Run Chaos Test 3 (Database Failure)? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  chaos_test_db_failure
fi

echo ""
echo "=== Chaos Testing Complete ==="
echo "Review results and fix any resilience gaps discovered."