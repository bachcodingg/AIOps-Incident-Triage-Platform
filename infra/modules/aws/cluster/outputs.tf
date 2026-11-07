output "name" {
  value = aws_eks_cluster.this.name
}

output "endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "oidc_provider_arn" {
  description = "Federated principal for IRSA trust policies."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "oidc_issuer" {
  description = "Issuer without the scheme, as IAM condition keys expect it."
  value       = local.oidc_issuer
}
