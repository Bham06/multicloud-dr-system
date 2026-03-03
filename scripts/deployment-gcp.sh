#!/bin/bash

VM_NAME=""
ZONE="us-eas1-b"
ACTION=$1

# Input validation
if [ "$ACTION" != "start" ] && [ "$ACTION" != "stop" ]; then
    echo "Usage: $0 <start|stop>"
    exit 1
fi

# Execute the GCP CLI command
if [ "$ACTION" = "start" ]; then
    echo "Starting instance $VM_NAME"
    gcloud compute ssh $VM_NAME --zone=$ZONE
else
    echo "Stopping instance $VM_NAME"
    gcloud compute ssh $VM_NAME --zone=$ZONE

# Check if the command succeeded
if [ $? -eq 0 ]; then
    echo "Command executed successfully"
else
    echo "Failed to start VM"
fi
