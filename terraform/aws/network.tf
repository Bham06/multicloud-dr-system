# VPC
resource "aws_vpc" "main" {
  cidr_block           = "10.1.1.0/26"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "vpc-dr-secondary"
  }
}

# Internet Gateway
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "igw-dr-secondary"
  }
}

# ===========================
#     Public Subnet
# ===========================

# EC2 Subnet
resource "aws_subnet" "app" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.1.1.0/28"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true

  tags = {
    Name = "subnet-app"
  }
}

# Route Table
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "rt-public"
  }
}

# Associate route table with subnet
resource "aws_route_table_association" "app" {
  subnet_id      = aws_subnet.app.id
  route_table_id = aws_route_table.public.id
}

# ==============
# Private Subnet
# ==============

# RDS Subnet
resource "aws_subnet" "db_1" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.1.1.16/28"
  availability_zone = "${var.region}a"

  tags = {
    Name = "subnet-db-1"
  }
}

# Second DB subnet 
resource "aws_subnet" "db_2" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.1.1.32/28"
  availability_zone = "${var.region}b"

  tags = {
    Name = "subnet-db-2"
  }
}

# Private route table
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "rt-private"
  }
}

# Associate route table for private subnets
resource "aws_route_table_association" "app_a" {
  subnet_id      = aws_subnet.db_1.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "app_b" {
  subnet_id      = aws_subnet.db_2.id
  route_table_id = aws_route_table.private.id
}

# ====================
# Security Groups
# ====================

# Security Group for EC2
resource "aws_security_group" "app" {
  name        = "dr-app-sg"
  description = "Security group for DR application"
  vpc_id      = aws_vpc.main.id

  # Allow HTTP
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow HTTP from anywhere"
  }

  # Allow HTTPS
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow HTTPS from anywhere"
  }

  # Allow SSH from IP
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # Allow SSH from anywhere temporarily
    description = "Allow SSH from EC2 Elastic IP"
  }

  # Allow traffic from GCP VPC via VPN
  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.0.1.0/24"]
    description = "All traffic from GCP via VPN"
  }

  # Allow all outbound
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "sg-dr-app"
  }
}

# Security Group for RDS
resource "aws_security_group" "db" {
  name        = "dr-db-sg"
  description = "Security group for RDS database"
  vpc_id      = aws_vpc.main.id

  # Allow PostgreSQL from app security group
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
    description     = "Allow PostgreSQL from app"
  }

  # Allow PostgreSQL from anywhere (for GCP)
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.0.1.0/24"]
    description = "Allow PostgreSQL from anywhere"
  }

  # Outbound for replication
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "sg-dr-db"
  }
}
