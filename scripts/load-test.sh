#!/bin/bash
# Load Testing with Apache Bench (ab)

LB_IP="34.36.128.254"

echo "=== Load Testing Suite ==="

# Test 1: Baseline (10 concurrent users)
echo "Test 1: Baseline - 10 concurrent users, 1000 requests"
ab -n 1000 -c 10 -g baseline.tsv http://$LB_IP/ | grep "Requests per second"

# Test 2: Moderate load (50 concurrent)
echo ""
echo "Test 2: Moderate - 50 concurrent users, 5000 requests"
ab -n 5000 -c 50 -g moderate.tsv http://$LB_IP/ | grep "Requests per second"

# Test 3: Heavy load (100 concurrent)
echo ""
echo "Test 3: Heavy - 100 concurrent users, 10000 requests"
ab -n 10000 -c 100 -g heavy.tsv http://$LB_IP/ | grep "Requests per second"

# Test 4: Spike test (rapid increase)
echo ""
echo "Test 4: Spike test - sudden traffic burst"
ab -n 1000 -c 200 http://$LB_IP/ | grep "Failed requests"

# Analysis
echo ""
echo "=== Results Analysis ==="
echo "Check TSV files for detailed latency percentiles"
echo "  - baseline.tsv"
echo "  - moderate.tsv"
echo "  - heavy.tsv"