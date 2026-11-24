output "key_ring_id" {
  value = google_kms_key_ring.ring.id
}

output "approval_token_signer_key_id" {
  description = "Crypto key id; the ledger signs with its primary version (JWS kid = key version name)."
  value       = google_kms_crypto_key.approval_token_signer.id
}

output "approval_token_signer_version" {
  description = "PM_AGENT_APPROVAL_KEY: first key version created with the key (rotate by adding versions + updating this)."
  value       = "${google_kms_crypto_key.approval_token_signer.id}/cryptoKeyVersions/1"
}

output "verifier_report_signer_key_id" {
  value = google_kms_crypto_key.verifier_report_signer.id
}

output "verifier_report_signer_version" {
  description = "PM_AGENT_VERIFIER_KEY."
  value       = "${google_kms_crypto_key.verifier_report_signer.id}/cryptoKeyVersions/1"
}

output "sdp_wrap_key_id" {
  value = google_kms_crypto_key.sdp_wrap.id
}

output "cmek_key_ids" {
  description = "CMEK key short name -> crypto key id."
  value       = { for k, v in google_kms_crypto_key.cmek : k => v.id }
}

output "cmek_grants_ready" {
  description = "Depend on this before creating CMEK-protected resources."
  value       = [for g in google_kms_crypto_key_iam_member.cmek : g.etag]
}
