# Cloud KMS (docs/10 §7, docs/07 §6, docs/08 §10): one key ring per env holding
#   * approval-token-signer  ASYMMETRIC_SIGN EC_SIGN_P256_SHA256 (JWS ES256)  -> signer: mcp-ledger SA ONLY
#   * verifier-report-signer ASYMMETRIC_SIGN EC_SIGN_P256_SHA256              -> signer: mcp-verifier SA ONLY
#   * sdp-wrap               ENCRYPT_DECRYPT (wraps the SDP pseudonymization data key)
#   * cmek-<service>         ENCRYPT_DECRYPT CMEK keys, one per consuming service
# Signer roles use authoritative *_iam_binding resources so that no other principal (including humans) can hold
# roles/cloudkms.signer on these keys through Terraform-managed policy; manual grants are reverted on apply.

resource "google_kms_key_ring" "ring" {
  project  = var.project_id
  name     = "pm-agent-${var.env}"
  location = var.region
}

# ---------------------------------------------------------------- signing keys (HSM, non-rotating by design)
resource "google_kms_crypto_key" "approval_token_signer" {
  name                       = "approval-token-signer"
  key_ring                   = google_kms_key_ring.ring.id
  purpose                    = "ASYMMETRIC_SIGN"
  destroy_scheduled_duration = "2592000s"
  labels                     = merge(var.labels, { purpose = "approval-token" })

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = var.protection_level
  }
}

resource "google_kms_crypto_key" "verifier_report_signer" {
  name                       = "verifier-report-signer"
  key_ring                   = google_kms_key_ring.ring.id
  purpose                    = "ASYMMETRIC_SIGN"
  destroy_scheduled_duration = "2592000s"
  labels                     = merge(var.labels, { purpose = "verifier-report" })

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = var.protection_level
  }
}

resource "google_kms_crypto_key" "sdp_wrap" {
  name                       = "sdp-wrap"
  key_ring                   = google_kms_key_ring.ring.id
  purpose                    = "ENCRYPT_DECRYPT"
  destroy_scheduled_duration = "2592000s"
  labels                     = merge(var.labels, { purpose = "sdp-pseudonymization" })

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = var.protection_level
  }
}

# ---------------------------------------------------------------- CMEK keys
resource "google_kms_crypto_key" "cmek" {
  for_each                   = var.cmek_keys
  name                       = "cmek-${each.key}"
  key_ring                   = google_kms_key_ring.ring.id
  purpose                    = "ENCRYPT_DECRYPT"
  rotation_period            = var.cmek_rotation_period
  destroy_scheduled_duration = "2592000s"
  labels                     = merge(var.labels, { purpose = "cmek", consumer = each.key })

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = var.protection_level
  }
}

# ---------------------------------------------------------------- signer IAM (authoritative per role)
resource "google_kms_crypto_key_iam_binding" "approval_token_signer" {
  crypto_key_id = google_kms_crypto_key.approval_token_signer.id
  role          = "roles/cloudkms.signer"
  members       = var.approval_token_signers
}

resource "google_kms_crypto_key_iam_binding" "verifier_report_signer" {
  crypto_key_id = google_kms_crypto_key.verifier_report_signer.id
  role          = "roles/cloudkms.signer"
  members       = var.verifier_report_signers
}

# Every component verifies approval tokens / verification reports with the public keys.
locals {
  viewer_bindings = length(var.signing_key_viewers) == 0 ? {} : {
    "approval|roles/cloudkms.viewer"          = { key = google_kms_crypto_key.approval_token_signer.id, role = "roles/cloudkms.viewer" }
    "approval|roles/cloudkms.publicKeyViewer" = { key = google_kms_crypto_key.approval_token_signer.id, role = "roles/cloudkms.publicKeyViewer" }
    "report|roles/cloudkms.viewer"            = { key = google_kms_crypto_key.verifier_report_signer.id, role = "roles/cloudkms.viewer" }
    "report|roles/cloudkms.publicKeyViewer"   = { key = google_kms_crypto_key.verifier_report_signer.id, role = "roles/cloudkms.publicKeyViewer" }
  }
}

resource "google_kms_crypto_key_iam_binding" "signing_key_viewers" {
  for_each      = local.viewer_bindings
  crypto_key_id = each.value.key
  role          = each.value.role
  members       = var.signing_key_viewers
}

resource "google_kms_crypto_key_iam_binding" "sdp_wrap_decrypter" {
  count         = length(var.sdp_wrap_decrypters) > 0 ? 1 : 0
  crypto_key_id = google_kms_crypto_key.sdp_wrap.id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  members       = var.sdp_wrap_decrypters
}

# ---------------------------------------------------------------- CMEK grants to service agents
locals {
  cmek_grants = merge([
    for key, members in var.cmek_encrypter_decrypters : {
      for label, m in members : "${key}|${label}" => { key = key, member = m }
    }
  ]...)
}

resource "google_kms_crypto_key_iam_member" "cmek" {
  for_each      = local.cmek_grants
  crypto_key_id = google_kms_crypto_key.cmek[each.value.key].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = each.value.member
}
