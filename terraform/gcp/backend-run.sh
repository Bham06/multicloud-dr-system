#!/bin/bash
export PROJECT_ID="final-year-project1-484523"
export BUCKET_NAME="${PROJECT_ID}-terraform-state"

# Create bucket with versioning and encryption
gcloud storage buckets create gs://${BUCKET_NAME} \
  --project=${PROJECT_ID} \
  --location=us-central1 \
  --uniform-bucket-level-access

# Enable encryption (default is Google-managed, can use CMEK)
gcloud storage buckets update gs://${BUCKET_NAME} \
  --versioning

# Set lifecycle to keep 10 versions
cat > lifecycle.json << 'EOF'
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {
          "numNewerVersions": 10,
          "isLive": false
        }
      }
    ]
  }
}
EOF

gcloud storage buckets update gs://${BUCKET_NAME} \
  --lifecycle-file=lifecycle.json

# Block public access
gcloud storage buckets update gs://${BUCKET_NAME} \
  --public-access-prevention