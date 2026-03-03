#!/bin/bash 

gcloud compute instances create primary-app-vm \
    --zone=us-central1-a \
    --machine-type=f1-micro \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=10GB \
    --tags=http-server,https-server 

gcloud compute firewall-rules create allow-flask-app \
    --allow tcp:8080 \
    --target-tags=http-server 