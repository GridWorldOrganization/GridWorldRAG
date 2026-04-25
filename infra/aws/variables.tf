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

# Multi-user credentials. Preferred way to configure auth: supply a map
# of {username -> password} so the Lambda can authenticate any matched
# pair and forward the username to the daemon's per-user scope. When
# set, takes precedence over the legacy basic_user / basic_pass pair.
# Example in terraform.tfvars:
#   basic_users = {
#     tobisako = "..."
#     tobi2    = "..."
#   }
variable "basic_users" {
  type      = map(string)
  default   = {}
  sensitive = true
}
