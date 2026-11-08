# IRSA roles. Each trust policy names exactly one Kubernetes service account,
# so a compromised pod elsewhere in the cluster cannot assume these roles.

locals {
  service_accounts = {
    external-secrets = { namespace = "external-secrets", service_account = "external-secrets" }
    loki             = { namespace = "monitoring", service_account = "loki" }
    remediator       = { namespace = "aiops", service_account = "remediator" }
  }

  policies = {
    external-secrets = {
      Version = "2012-10-17"
      Statement = [{
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        # The trailing wildcard is what scopes this role to the prefix.
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${var.secret_prefix}*"
      }]
    }

    loki = {
      Version = "2012-10-17"
      Statement = [
        {
          Effect   = "Allow"
          Action   = ["s3:ListBucket"]
          Resource = aws_s3_bucket.loki.arn
        },
        {
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
          Resource = "${aws_s3_bucket.loki.arn}/*"
        },
      ]
    }

    # Kubernetes RBAC carries the remediator's real power (week 9). On AWS it
    # may only look at nodes, for example to confirm a spot interruption.
    remediator = {
      Version = "2012-10-17"
      Statement = [{
        Sid      = "DescribeOnly"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus"]
        Resource = "*" # these EC2 Describe calls do not support resource-level scoping
      }]
    }
  }
}

resource "aws_iam_role" "irsa" {
  for_each = local.service_accounts

  name = "${var.name}-${each.key}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.oidc_issuer}:aud" = "sts.amazonaws.com"
          "${var.oidc_issuer}:sub" = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "irsa" {
  for_each = local.service_accounts

  name   = each.key
  role   = aws_iam_role.irsa[each.key].id
  policy = jsonencode(local.policies[each.key])
}

# --- Loki chunks --------------------------------------------------------------

resource "aws_s3_bucket" "loki" {
  #checkov:skip=CKV_AWS_144:Logs are disposable; no cross-region copy.
  #checkov:skip=CKV_AWS_18:No access logging on a log bucket used for a few hours.
  #checkov:skip=CKV2_AWS_62:No consumer for bucket event notifications.
  #checkov:skip=CKV_AWS_145:SSE-S3 is enough for synthetic workload logs.
  #checkov:skip=CKV_AWS_21:Chunks are immutable and expire; versioning would only keep deleted data around.
  bucket_prefix = "${var.name}-loki-"
  force_destroy = true # logs must not block `make destroy`
}

resource "aws_s3_bucket_server_side_encryption_configuration" "loki" {
  bucket = aws_s3_bucket.loki.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "loki" {
  bucket = aws_s3_bucket.loki.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "loki" {
  bucket = aws_s3_bucket.loki.id

  rule {
    id     = "expire-chunks"
    status = "Enabled"

    filter {}

    expiration {
      days = var.loki_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
