resource "juju_offer" "this" {
  for_each         = var.offered_endpoints
  model_uuid       = local.model_uuid
  name             = "${module.valkey.app_name}-${each.value}"
  application_name = module.valkey.app_name
  endpoints        = [each.value]
}
