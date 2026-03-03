#!/bin/bash
 
INSTANCE_ID="i-0b00c3e98b3caff18"
ACTION=$1

# Input validation
if [ "$ACTION" != "start" ] && [ "$ACTION" != "stop"]; then
    echo "Usage: $0 <start|stop>"
    exit 1
fi

# Execute CLI command
if [ "$ACTION" = "start" ]; then
    echo "Starting instance $INSTANCE_ID..."
    aws ec2 start-instances --instance_ids $INSTANCE_ID
else
    echo "Stopping instance $INSTANCE_ID..."
    aws ec2 stop-instances --instance-ids $INSTANCE_ID
fi

# Check if command executed
if [ $? -eq 0 ]; then
   echo "Command executed successfully"
else
   echo "Command failed. Check AWS permission and configuration."
fi