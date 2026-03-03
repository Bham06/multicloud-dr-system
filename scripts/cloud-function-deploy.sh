#!/bin/bash
# Secret manager secret for S3 connectin string
echo -n "DefaultEndpointsProtocol=http;AccountName=..." | \


# Deploy cloud function
cd functions/gcs-to-s3

PROJECT_ID = 

gcloud function deploy sync-to-aws \
   --runtime=python39 \
   --trigger-bucket=${PROJECT_ID}-dr-primary \
   --entry-point=sync_file \
   --timeout=60s \
   --memory=512MB \
   --set-secrets