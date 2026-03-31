# ================================================
# AWS VPN Configuration (Dual Tunnels for HA)
# ================================================

# Customer Gateway (Represents GCP VPN Gateway Interface 0)
resource "aws_customer_gateway" "gcp_interface0" {
  bgp_asn    = 65000
  ip_address = var.gcp_vpn_gateway_interface0_ip
  type       = "ipsec.1"

  tags = {
    Name = "gcp-customer-gateway-interface0"
  }
}

# Customer Gateway (Represents GCP VPN Gateway Interface 1)
resource "aws_customer_gateway" "gcp_interface1" {
  bgp_asn    = 65000
  ip_address = var.gcp_vpn_gateway_interface1_ip
  type       = "ipsec.1"

  tags = {
    Name = "gcp-customer-gateway-interface1"
  }
}

# Virtual Private Gateway
resource "aws_vpn_gateway" "main" {
  vpc_id          = aws_vpc.main.id
  amazon_side_asn = 64512

  tags = {
    Name = "dr-vpn-gateway"
  }
}

# VPN Gateway Attachment
resource "aws_vpn_gateway_attachment" "vpn_attachment" {
  vpc_id         = aws_vpc.main.id
  vpn_gateway_id = aws_vpn_gateway.main.id
}

# ============================================
# VPN Connection 1: To GCP Interface 0
# ============================================
resource "aws_vpn_connection" "gcp_tunnel1" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.gcp_interface0.id
  type                = "ipsec.1"
  static_routes_only  = false

  # Tunnel 1 configuration
  tunnel1_inside_cidr   = "169.254.10.0/30"
  tunnel1_preshared_key = var.vpn_shared_secret

  tags = {
    Name = "vpn-connection-to-gcp-interface0"
  }
}

# ============================================
# VPN Connection 2: To GCP Interface 1
# ============================================
resource "aws_vpn_connection" "gcp_tunnel2" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.gcp_interface1.id
  type                = "ipsec.1"
  static_routes_only  = false

  # Tunnel 2 configuration 
  tunnel2_inside_cidr   = "169.254.10.4/30"
  tunnel2_preshared_key = var.vpn_shared_secret

  tags = {
    Name = "vpn-connection-to-gcp-interface1"
  }
}

resource "aws_vpn_gateway_route_propagation" "private_routes" {
  vpn_gateway_id = aws_vpn_gateway.main.id
  route_table_id = aws_route_table.private.id
}

resource "aws_vpn_gateway_route_propagation" "public_routes" {
  vpn_gateway_id = aws_vpn_gateway.main.id
  route_table_id = aws_route_table.public.id
}

# RDS security group to allow PostgreSQL from GCP VPC
resource "aws_security_group_rule" "db_from_gcp_vpn" {
  type              = "ingress"
  from_port         = 5432
  to_port           = 5432
  protocol          = "tcp"
  cidr_blocks       = [var.gcp_vpc_cidr]
  security_group_id = aws_security_group.db.id
  description       = "PostgreSQL from GCP VPC via VPN"
}
