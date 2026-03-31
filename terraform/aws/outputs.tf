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

output "aws_vpn_tunnel1_address" {
  description = "AWS VPN Connection 1 Tunnel 1 Address (for GCP interface 0)"
  value       = aws_vpn_connection.gcp_tunnel1.tunnel1_address
}

output "aws_vpn_tunnel2_address" {
  description = "AWS VPN Connection 2 Tunnel 1 Address (for GCP interface 1)"
  value       = aws_vpn_connection.gcp_tunnel2.tunnel1_address
}

# output "vpn_connection_id" {
#   description = "AWS VPN Connection ID"
#   value       = aws_vpn_connection.gcp.id
# }

# output "vpn_tunnel_status" {
#   description = "VPN Tunnel Status"
#   value = {
#     tunnel1_status = aws_vpn_connection.gcp.tunnel1_status
#   }
# }
