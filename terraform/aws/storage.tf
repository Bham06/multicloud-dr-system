# S3 Bucket
resource "aws_s3_bucket" "secondary" {
  bucket = "dr-storage-secondary-${random_string.suffix.result}"

  tags = {
    Name = "dr-storage-secondary"
  }
}

# Random suffix for unique bucket name
resource "random_string" "suffix" {
  length  = 8
  special = false
  upper   = false
}

# Versioning
resource "aws_s3_bucket_versioning" "secondary" {
  bucket = aws_s3_bucket.secondary.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Lifecycle rule.
# Mirrors the 30-day deletion on the GCS source bucket. Without this the
# replicated copies accumulate indefinitely, so the secondary's storage cost
# grows without bound while the primary's stays flat.
resource "aws_s3_bucket_lifecycle_configuration" "secondary" {
  bucket = aws_s3_bucket.secondary.id

  rule {
    id     = "delete-old-backups"
    status = "Enabled"

    filter {
      prefix = "backups/"
    }

    expiration {
      days = 30
    }

    # Versioning is enabled, so expiring the current object only hides it
    # behind a delete marker. This is what actually reclaims the storage.
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # Must be its own rule: AWS rejects expired_object_delete_marker alongside
  # days in a single expiration block.
  rule {
    id     = "clean-expired-delete-markers"
    status = "Enabled"

    filter {
      prefix = "backups/"
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  # Incomplete uploads are invisible in the console but still billed.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.secondary]
}

# Block public access
resource "aws_s3_bucket_public_access_block" "secondary" {
  bucket = aws_s3_bucket.secondary.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# IAM Policy for S3 access
resource "aws_iam_policy" "s3_backup_access" {
  name        = "dr-s3-backup-access"
  description = "Allow EC2 to read backups from S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.secondary.arn,
          "${aws_s3_bucket.secondary.arn}/*"
        ]
      }
    ]
  })
}

# Attach S3 policy to role
resource "aws_iam_role_policy_attachment" "ec2_s3_access" {
  role       = aws_iam_role.ec2_app_role.name
  policy_arn = aws_iam_policy.s3_backup_access.arn
}

