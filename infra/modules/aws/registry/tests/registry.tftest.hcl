mock_provider "aws" {}

variables {
  repositories = ["incidentlens/gateway", "incidentlens/catalog"]
}

run "repositories_are_immutable_and_scanned" {
  command = plan

  assert {
    condition     = length(aws_ecr_repository.this) == 2
    error_message = "expected one repository per service"
  }

  assert {
    condition     = alltrue([for r in aws_ecr_repository.this : r.image_tag_mutability == "IMMUTABLE"])
    error_message = "a tag must always mean the same image"
  }

  assert {
    condition     = alltrue([for r in aws_ecr_repository.this : r.image_scanning_configuration[0].scan_on_push])
    error_message = "images must be scanned on push"
  }
}
