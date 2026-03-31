# DB Subnet Group
resource "aws_db_subnet_group" "main" {
  name       = "db-subnet-group"
  subnet_ids = [aws_subnet.db_1.id, aws_subnet.db_2.id]

  tags = {
    Name = "db-subnet-group"
  }
}

# RDS Instance
resource "aws_db_instance" "main" {
  identifier = "dr-secondary-db"

  # Engine
  engine         = "postgres"
  engine_version = "14"

  # Instance
  instance_class    = "db.t3.micro"
  allocated_storage = 20
  storage_type      = "gp2"

  # Database
  db_name  = "application"
  username = var.db_user
  password = var.db_password

  # Networking
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = true # For GCP backup restore 

  # Backup
  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "mon:04:00-mon:05:00"

  # High availabilty
  multi_az = false

  # Deletion
  skip_final_snapshot = true
  deletion_protection = false
  # final_snapshot_identifier = "snapshot-v3"

  # Performance Insights
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  parameter_group_name = aws_db_parameter_group.replication.name

  tags = {
    Name = "dr-secondary-db"
  }
}

# Parameter Group for replication 
resource "aws_db_parameter_group" "replication" {
  name   = "dr-replication-params"
  family = "postgres14"

  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_replication_slots"
    value        = "10"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_wal_senders"
    value        = "10"
    apply_method = "pending-reboot"
  }
}
