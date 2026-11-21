terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

variable "project_id" {
  description = "GCP Project ID"
  type        = string
  default     = "csv-analytics-cloud"
}

variable "region" {
  description = "GCP Region for low latency and student quota"
  type        = string
  default     = "asia-southeast1" # Singapore
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Private Storage Bucket for Raw CSV and Summary Artifacts
resource "google_storage_bucket" "csv_storage" {
  name                        = "${var.project_id}-csv-storage"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  versioning {
    enabled = true
  }
}

# 2. Managed PostgreSQL Instance (Cloud SQL - Minimal Tier for Student Budget)
resource "google_sql_database_instance" "postgres" {
  name             = "csv-analytics-db"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier              = "db-f1-micro" # Lowest cost tier (~$9/month, covered by free credit)
    availability_type = "ZONAL"
    disk_size         = 10
    disk_type         = "PD_SSD"

    backup_configuration {
      enabled = false # Disabled to minimize storage fees in student lab
    }

    ip_configuration {
      ipv4_enabled = true # Can be switched to private IP with Serverless VPC Connector
    }
  }
}

resource "google_sql_database" "database" {
  name     = "csv_analytics"
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "db_user" {
  name     = "clouduser"
  instance = google_sql_database_instance.postgres.name
  password = "SecureDbPassword2026!"
}

# 3. Cloud Run Service (Serverless Container for API & Worker)
resource "google_cloud_run_service" "api_service" {
  name     = "csv-analytics-api"
  location = var.region

  template {
    spec {
      containers {
        image = "gcr.io/${var.project_id}/csv-analytics:latest"
        
        resources {
          limits = {
            memory = "512Mi"
            cpu    = "1000m"
          }
        }

        env {
          name  = "DATABASE_URL"
          value = "postgresql://clouduser:SecureDbPassword2026!@${google_sql_database_instance.postgres.public_ip_address}:5432/csv_analytics"
        }
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }
}

# Public access policy for the API service
resource "google_cloud_run_service_iam_member" "public_access" {
  service  = google_cloud_run_service.api_service.name
  location = google_cloud_run_service.api_service.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "service_url" {
  description = "The deployed HTTPS entry point"
  value       = google_cloud_run_service.api_service.status[0].url
}

output "database_ip" {
  description = "Cloud SQL Public/Private IP"
  value       = google_sql_database_instance.postgres.public_ip_address
}
