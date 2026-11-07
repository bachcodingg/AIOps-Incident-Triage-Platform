mock_provider "aws" {}

variables {
  name               = "test"
  cluster_name       = "test"
  availability_zones = ["eu-central-1a", "eu-central-1b"]
}

run "subnets_do_not_overlap_and_carry_discovery_tags" {
  command = plan

  assert {
    condition = (
      length(distinct(concat(aws_subnet.public[*].cidr_block, aws_subnet.private[*].cidr_block))) == 4
    )
    error_message = "public and private CIDRs must be distinct"
  }

  assert {
    condition     = aws_subnet.public[1].cidr_block == "10.40.16.0/20" && aws_subnet.private[0].cidr_block == "10.40.32.0/20"
    error_message = "unexpected subnet layout"
  }

  assert {
    condition     = aws_subnet.public[0].tags["kubernetes.io/role/elb"] == "1"
    error_message = "public subnets must be discoverable for internet-facing load balancers"
  }

  assert {
    condition     = aws_subnet.private[0].tags["kubernetes.io/role/internal-elb"] == "1"
    error_message = "private subnets must be discoverable for internal load balancers"
  }
}

run "subnets_never_hand_out_public_ips" {
  command = plan

  assert {
    condition     = alltrue([for s in aws_subnet.public : !s.map_public_ip_on_launch])
    error_message = "nodes live in private subnets; nothing should get a public IP by default"
  }
}

run "rejects_a_single_az" {
  command = plan

  variables {
    availability_zones = ["eu-central-1a"]
  }

  expect_failures = [var.availability_zones]
}
