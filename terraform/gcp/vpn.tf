# ===============================================
#  GCP VPN CONFIGURATION FOR AWS CONNECTIVITY
# ===============================================

# High availabilty VPN Gateway
resource "google_compute_ha_vpn_gateway" "gcp_vpn" {
  name    = "dr-vpn-gateway"
  region  = var.region
  network = google_compute_network.vpc.id
}

# Cloud Router for BGP
resource "google_compute_router" "vpn_router" {
  name    = "dr-vpn-router"
  region  = var.region
  network = google_compute_network.vpc.id

  bgp {
    asn               = 65000
    advertise_mode    = "CUSTOM"
    advertised_groups = ["ALL_SUBNETS"]

    # Displaying GCP subnets to AWS
    advertised_ip_ranges {
      range = google_compute_subnetwork.subnet.ip_cidr_range
    }
  }
}

# External VPN Gateway (AWS VPN Endpoint)
resource "google_compute_external_vpn_gateway" "aws_vpn" {
  name            = "aws-vpn-gateway"
  redundancy_type = "TWO_IPS_REDUNDANCY"
  description     = "AWS VPN Gateway Endpoints"

  interface {
    id         = 0
    ip_address = var.aws_vpn_tunnel1_ip
  }

  interface {
    id         = 1
    ip_address = var.aws_vpn_tunnel2_ip
  }
}

# ==============================================
# Tunnel 1 : GCP interface 0 -> AWS tunnel 1
# ==============================================
resource "google_compute_vpn_tunnel" "tunnel1_to_aws" {
  name                            = "tunnel1-to-aws"
  region                          = var.region
  vpn_gateway                     = google_compute_ha_vpn_gateway.gcp_vpn.id
  peer_external_gateway           = google_compute_external_vpn_gateway.aws_vpn.id
  peer_external_gateway_interface = 0
  shared_secret                   = var.shared_secret
  router                          = google_compute_router.vpn_router.id
  vpn_gateway_interface           = 0
  ike_version                     = 2
}

# BGP Session for tunnel 1
resource "google_compute_router_interface" "vpn_interface" {
  name       = "vpn-interface1-to-aws"
  router     = google_compute_router.vpn_router.name
  region     = var.region
  ip_range   = "169.254.10.1/30"
  vpn_tunnel = google_compute_vpn_tunnel.tunnel1_to_aws.name
}

resource "google_compute_router_peer" "aws_bgp_peer" {
  name                      = "aws-bgp-peer"
  router                    = google_compute_router.vpn_router.name
  region                    = var.region
  peer_ip_address           = "169.254.10.2"
  peer_asn                  = 64512
  advertised_route_priority = 100
  interface                 = google_compute_router_interface.vpn_interface.name
}

# ==============================================
# Tunnel 2 : GCP interface 1 -> AWS tunnel 2
# ==============================================
resource "google_compute_vpn_tunnel" "tunnel2_to_aws" {
  name                            = "tunnel2-to-aws"
  region                          = var.region
  vpn_gateway                     = google_compute_ha_vpn_gateway.gcp_vpn.id
  peer_external_gateway           = google_compute_external_vpn_gateway.aws_vpn.id
  peer_external_gateway_interface = 1
  shared_secret                   = var.shared_secret
  router                          = google_compute_router.vpn_router.id
  vpn_gateway_interface           = 1
  ike_version                     = 2
}

# BGP Session for tunnel 2
resource "google_compute_router_interface" "vpn_interface2" {
  name       = "vpn-interface2-to-aws"
  router     = google_compute_router.vpn_router.name
  region     = var.region
  ip_range   = "169.254.10.5/30"
  vpn_tunnel = google_compute_vpn_tunnel.tunnel2_to_aws.name
}

resource "google_compute_router_peer" "aws_bgp_peer2" {
  name                      = "aws-bgp-peer2"
  router                    = google_compute_router.vpn_router.name
  region                    = var.region
  peer_ip_address           = "169.254.10.6"
  peer_asn                  = 64512
  advertised_route_priority = 100
  interface                 = google_compute_router_interface.vpn_interface2.name
}

# Firewall Rule
resource "google_compute_firewall" "allow_from_aws_vpn" {
  name    = "dr-allow-from-aws-vpn"
  network = google_compute_network.vpc.id

  description = "Allow all traffic from AWS via VPN tunnel"

  allow {
    protocol = "tcp"
  }

  allow {
    protocol = "udp"
  }

  allow {
    protocol = "icmp"
  }

  source_ranges = [var.aws_vpc_cidr]

  priority = 1000
}
