# A VPC with no way out. The sandbox and preview functions run AI-written code, so they live
# here: no internet gateway, no NAT, so no route to the internet. The only door is an S3 gateway
# endpoint that can fetch merged code for the preview (free; no NAT bill).
# The API and worker stay outside the VPC: they need the internet (model API, database).

data "aws_availability_zones" "up" {
  state = "available"
}

resource "aws_vpc" "sealed" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-sealed" }
}

resource "aws_subnet" "sealed" {
  count             = 2
  vpc_id            = aws_vpc.sealed.id
  cidr_block        = cidrsubnet(aws_vpc.sealed.cidr_block, 8, count.index)
  availability_zone = data.aws_availability_zones.up.names[count.index]
  tags              = { Name = "${var.name}-sealed-${count.index}" }
}

# Only the implicit local route, plus the S3 endpoint below.
resource "aws_route_table" "sealed" {
  vpc_id = aws_vpc.sealed.id
  tags   = { Name = "${var.name}-sealed" }
}

resource "aws_route_table_association" "sealed" {
  count          = 2
  subnet_id      = aws_subnet.sealed[count.index].id
  route_table_id = aws_route_table.sealed.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.sealed.id
  service_name      = "com.amazonaws.${local.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.sealed.id]

  # Even with stolen credentials, this endpoint only reads merged previews.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = ["s3:GetObject"]
      Resource  = ["${aws_s3_bucket.previews.arn}/previews/*"]
    }]
  })
}

# Terraform removes AWS's default allow-all egress rule, so a group with no egress rules
# lets nothing out.
resource "aws_security_group" "sandbox" {
  name        = "${var.name}-sandbox"
  description = "Sandbox: no inbound, no outbound"
  vpc_id      = aws_vpc.sealed.id
}

resource "aws_security_group" "preview" {
  name        = "${var.name}-preview"
  description = "Preview: HTTPS to S3 only"
  vpc_id      = aws_vpc.sealed.id
}

resource "aws_vpc_security_group_egress_rule" "preview_s3" {
  security_group_id = aws_security_group.preview.id
  description       = "S3 through the gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
}
