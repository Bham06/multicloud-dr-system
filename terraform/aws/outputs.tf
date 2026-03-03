output "aws_instance_public_ip" {
  value = aws_eip.app.public_ip
}

output "aws_instance_public_dns" {
  value = aws_eip.app.public_dns
}

output "rds_endpoint" {
  value = aws_db_instance.main.endpoint
}

output "s3_bucket_name" {
  value = aws_s3_bucket.secondary.id
}
