terraform {
  required_version = ">= 1.11"

  required_providers {
    # 2.4.1 accepts the null write-only values that secrets get on runs without credentials.
    juju = {
      source  = "juju/juju"
      version = ">= 2.4.1"
    }
  }
}
