terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.6.0, < 9.0.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 8.6.0, < 9.0.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.5.0"
    }
  }

  # Partial configuration: terraform init -backend-config=backend.hcl (see backend.hcl.example).
  backend "gcs" {}
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}
