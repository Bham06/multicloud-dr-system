# Secrets Manager secret for the RDS database password.
# The EC2 instance fetches this at runtime — the plaintext password is never
# written into user-data or any log file.
resource "aws_secretsmanager_secret" "db_password" {
  name                    = "dr/db-password"
  recovery_window_in_days = 0

  tags = {
    Name = "dr-db-password"
  }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = var.db_password
}

# Allow the EC2 application role to read the DB password secret only.
resource "aws_iam_role_policy" "ec2_read_db_secret" {
  name = "dr-ec2-read-db-secret"
  role = aws_iam_role.ec2_app_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.db_password.arn]
      }
    ]
  })
}
