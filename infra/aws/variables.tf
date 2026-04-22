variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "aws_profile" {
  type    = string
  default = "sandbox2"
}

# Credentials for the public Lambda URL's embedded Basic Auth check.
# No defaults on purpose — an accidental `terraform apply` without
# terraform.tfvars should fail, not silently ship a weak default into
# Lambda env. Keep the real values in infra/aws/terraform.tfvars, which is
# gitignored.
variable "basic_user" {
  type      = string
  sensitive = true
}

variable "basic_pass" {
  type      = string
  sensitive = true
}
