# Elastic IP for Internet NEG
resource "aws_eip" "app" {
  domain = "vpc"

  tags = {
    Name = "eip-dr-app"
  }
}

# Key Pair
resource "aws_key_pair" "app" {
  key_name   = "dr-app-key"
  public_key = file("~/.ssh/id_ed25519.pub")
}

# EC2 Role
resource "aws_iam_role" "ec2_app_role" {
  name = "dr-ec2-app-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

# Instance profile
resource "aws_iam_instance_profile" "ec2_app_profile" {
  name = "dr-ec2-app-profile"
  role = aws_iam_role.ec2_app_role.name
}

# EC2 Instance
resource "aws_instance" "app" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = "t3.micro"

  subnet_id                   = aws_subnet.app.id
  vpc_security_group_ids      = [aws_security_group.app.id]
  key_name                    = aws_key_pair.app.key_name
  associate_public_ip_address = true

  iam_instance_profile = aws_iam_instance_profile.ec2_app_profile.name

  # User data (cloud-init)
  user_data = templatefile("${path.module}/scripts/user-data.sh", {
    db_host        = aws_db_instance.main.address
    db_port        = aws_db_instance.main.port
    db_name        = aws_db_instance.main.db_name
    db_user        = aws_db_instance.main.username
    db_password    = var.db_password
    s3_bucket_name = aws_s3_bucket.secondary.id
    aws_region     = var.region
  })

  user_data_replace_on_change = true

  tags = {
    Name = "dr-app-secondary"
    Role = "app-and-restore"
  }
}

# Associate Elastic IP with instance
resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}

# Get latest Ubuntu AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}
