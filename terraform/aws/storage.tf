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

# Lifecycle rule
# resource "aws_s3_bucket_lifecycle_configuration" "secondary" {
#   bucket = aws_s3_bucket.secondary.id

#   rule {
#     id     = "delete-old-backups"
#     status = "Enabled"

#     expiration {
#       days = 30
#     }
#   }
# }

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

