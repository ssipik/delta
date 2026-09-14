variable "image_repo" {
  type    = string
  default = "getregistry.azurecr.io/rag_delta"
}
variable "image_tag" {
  type = string
}

# Stage 2 (DELTA). Registered by bitbucket-pipelines.yml, started by Airflow after discover:
#   nomad job dispatch -meta run_id=<airflow run_id> rag-delta-template
job "rag-delta-template" {
  datacenters = ["dc1"]
  type        = "batch"

  parameterized {
    # The same Airflow run_id discover got; delta diffs that run's manifest.
    meta_required = ["run_id"]
  }

  group "delta" {
    count = 1

    restart {
      attempts = 0
      mode     = "fail"
    }
    # Retries are Airflow's job, not Nomad's.
    reschedule {
      attempts  = 0
      unlimited = false
    }

    task "delta" {
      driver = "docker"

      config {
        image   = "${var.image_repo}:${var.image_tag}"
        command = "python"
        # No volume: delta only compares Postgres tables, it never reads documents.
        args = ["-m", "rag_delta", "--run-id", "${NOMAD_META_run_id}"]
      }

      vault {}

      # Vault secrets + the rag-postgres address from Consul, rendered as env vars
      # (same pattern as rag-postgres.hcl), so the code only ever reads env vars.
      template {
        data        = <<EOT
{{- with secret "kv/data/secret/cluster/rag" }}
{{- $user := .Data.data.POSTGRES_USER }}{{ $pw := .Data.data.POSTGRES_PASSWORD }}
{{- range service "rag-postgres" }}
RAG_PG_DSN=postgresql+psycopg://{{ $user }}:{{ $pw | urlquery }}@{{ .Address }}:{{ .Port }}/rag_ingestion
{{- end }}
{{- end }}
EOT
        destination = "secrets/rag.env"
        env         = true
        # A batch run must not be restarted by a secret rotation mid-run.
        change_mode = "noop"
      }

      resources {
        cpu    = 1000
        memory = 1024
      }
    }
  }
}
