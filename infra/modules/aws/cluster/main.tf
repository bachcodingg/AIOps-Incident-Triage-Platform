# EKS control plane, a spot managed node group, the IAM OIDC provider that IRSA
# relies on, and the core add-ons.

locals {
  ec2_assume_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  eks_assume_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  oidc_issuer = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")

  node_policies = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
}

# --- control plane ------------------------------------------------------------

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-cluster"
  assume_role_policy = local.eks_assume_policy
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# Created here so it has a retention period and goes away with `destroy`.
# If EKS created it, it would never expire and would outlive the cluster.
resource "aws_cloudwatch_log_group" "cluster" {
  #checkov:skip=CKV_AWS_158:Control plane logs hold no secrets; a KMS key per session adds cost, not safety.
  #checkov:skip=CKV_AWS_338:Seven days is enough for sessions that last a few hours.
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = var.log_retention_days
}

resource "aws_kms_key" "secrets" {
  #checkov:skip=CKV2_AWS_64:The default key policy delegates to IAM, which is what the role policy below relies on.
  description             = "Envelope encryption of Kubernetes secrets in ${var.name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_iam_role_policy" "cluster_kms" {
  name = "secrets-encryption"
  role = aws_iam_role.cluster.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey", "kms:CreateGrant"]
      Resource = aws_kms_key.secrets.arn
    }]
  })
}

resource "aws_eks_cluster" "this" {
  #checkov:skip=CKV_AWS_339:Checkov's version list lags AWS; 1.37 is on EKS standard support until December 2027.
  #checkov:skip=CKV_AWS_38:Checkov cannot see through the variable; its validation rejects 0.0.0.0/0, and a test proves it.
  #checkov:skip=CKV_AWS_39:kubectl and the GitOps bootstrap run from a laptop; there is no VPN, so the endpoint stays public but allow-listed.
  name                      = var.name
  version                   = var.kubernetes_version
  role_arn                  = aws_iam_role.cluster.arn
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.public_access_cidrs
  }

  encryption_config {
    resources = ["secrets"]

    provider {
      key_arn = aws_kms_key.secrets.arn
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy.cluster_kms,
    aws_cloudwatch_log_group.cluster,
  ]
}

# --- IRSA ---------------------------------------------------------------------

# AWS trusts the EKS issuer through its own CA library, so no thumbprint is needed.
resource "aws_iam_openid_connect_provider" "this" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

# --- nodes --------------------------------------------------------------------

resource "aws_iam_role" "node" {
  name               = "${var.name}-node"
  assume_role_policy = local.ec2_assume_policy
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = local.node_policies

  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

resource "aws_eks_node_group" "spot" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "spot"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids
  capacity_type   = "SPOT"
  ami_type        = "AL2023_x86_64_STANDARD"
  instance_types  = var.node_instance_types

  scaling_config {
    desired_size = var.node_count
    min_size     = 1
    max_size     = var.node_max_count
  }

  update_config {
    max_unavailable = 1
  }

  # F14 (node loss) terminates a node; the group, not Terraform, replaces it.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# --- add-ons ------------------------------------------------------------------

resource "aws_eks_addon" "core" {
  for_each = toset(["vpc-cni", "kube-proxy", "coredns"])

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  # CoreDNS stays degraded until a node can schedule it.
  depends_on = [aws_eks_node_group.spot]
}

resource "aws_iam_role" "ebs_csi" {
  name = "${var.name}-ebs-csi"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.this.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
          "${local.oidc_issuer}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  service_account_role_arn    = aws_iam_role.ebs_csi.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.spot,
    aws_iam_role_policy_attachment.ebs_csi,
  ]
}
