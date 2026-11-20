# The state stores of Design 04, "State stores": the engine's database (event histories) and the session database
# (context log, ledger). One PostgreSQL instance, Multi-AZ, holds both. The design names Aurora; a Multi-AZ RDS
# instance gives the same failure behaviour (a synchronous standby in another zone and an automatic failover)
# at a fraction of the price.
resource "random_password" "db" {
  length  = 24
  special = false
}
resource "aws_db_subnet_group" "this" {
  name       = "${var.project}-db"
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "PostgreSQL from inside the VPC only"
  vpc_id      = aws_vpc.this.id
  ingress {
    description = "PostgreSQL from the VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  tags = { Name = "${var.project}-db" }
}
# Temporal's server connects without TLS in this build, so the instance must not force it. Everything is inside
# private subnets, reachable only from the VPC.
resource "aws_db_parameter_group" "this" {
  name   = "${var.project}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "0"
  }
}
resource "aws_db_instance" "this" {
  identifier                 = "${var.project}-db"
  engine                     = "postgres"
  engine_version             = "16"
  instance_class             = "db.t4g.small"
  allocated_storage          = 20
  storage_type               = "gp3"
  db_name                    = "postgres"
  username                   = "temporal"
  password                   = random_password.db.result
  multi_az                   = true
  db_subnet_group_name       = aws_db_subnet_group.this.name
  vpc_security_group_ids     = [aws_security_group.db.id]
  parameter_group_name       = aws_db_parameter_group.this.name
  publicly_accessible        = false
  backup_retention_period    = 1
  skip_final_snapshot        = true
  deletion_protection        = false
  apply_immediately          = true
  auto_minor_version_upgrade = false
}
# The one image that holds every service and the workers.
resource "aws_ecr_repository" "agent" {
  name         = "${var.project}/agent"
  force_delete = true
}
