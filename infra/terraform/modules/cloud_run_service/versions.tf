terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.6.0, < 9.0.0"
    }
    # functional_type (Agent Platform MCP-server registration) is Beta-only in the provider.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 8.6.0, < 9.0.0"
    }
  }
}
