locals {
  # Space per endpoint. The entry without endpoint is the default space.
  bindings = { for b in var.endpoint_bindings : coalesce(b.endpoint, "default") => b.space }

  charm_name    = "valkey"
  default_space = lookup(local.bindings, "default", null)
}
