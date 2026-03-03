#!/bin/bash

export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export STATE_BUCKET="${AWS_ACCOUNT_ID}-terraform-state"
export AWS_REGION="us-east-1" 

# Create S3 bucket with versioning and encryption
aws s3api create-bucket \
  --bucket ${STATE_BUCKET} \
  --region ${AWS_REGION} \
  
# Enable versioning
aws s3api put-bucket-versioning \
  --bucket ${STATE_BUCKET} \
  --versioning-configuration Status=Enabled

# Enable encryption
aws s3api put-bucket-encryption \
  --bucket ${STATE_BUCKET} \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      },
      "BucketKeyEnabled": true
    }]
  }'

# Block public access
aws s3api put-public-access-block \
  --bucket ${STATE_BUCKET} \
  --public-access-block-configuration '{
    "BlockPublicAcls": true,
    "IgnorePublicAcls": true,
    "BlockPublicPolicy": true,
    "RestrictPublicBuckets": true
  }'

# DynamoDB table for state locking
aws dynamodb create-table \
  --table-name terraform-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ${AWS_REGION}