variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "aws_profile" {
  type    = string
  default = "sandbox2"
}

variable "basic_user" {
  type      = string
  default   = "tobisako"
  sensitive = true
}

variable "basic_pass" {
  type      = string
  default   = "admin"
  sensitive = true
}
