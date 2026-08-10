#!/usr/bin/env bash
set -uo pipefail

repo="ningxia_migration"
index="openalex_full_20260706"
base_snapshot="openalex_full_20260706_ningxia_20260807"
state_dir="/var/lib/openalex-migration"
current_file="${state_dir}/current_snapshot"
attempt_file="${state_dir}/attempt"
success_file="${state_dir}/successful_snapshot"
es_url="http://127.0.0.1:9200"

mkdir -p "${state_dir}"

if [[ -s "${current_file}" ]]; then
  current_snapshot="$(<"${current_file}")"
else
  current_snapshot="${base_snapshot}"
  printf '%s\n' "${current_snapshot}" >"${current_file}"
fi

while true; do
  response="$(curl -fsS --retry 3 --retry-delay 5 --max-time 30 \
    "${es_url}/_snapshot/${repo}/${current_snapshot}" 2>/dev/null || true)"
  state="$(jq -r '.snapshots[0].state // empty' <<<"${response}" 2>/dev/null || true)"

  case "${state}" in
    SUCCESS)
      printf '%s\n' "${current_snapshot}" >"${success_file}"
      echo "snapshot supervisor: ${current_snapshot} completed successfully"
      exit 0
      ;;
    IN_PROGRESS|STARTED)
      sleep 30
      ;;
    FAILED|PARTIAL)
      attempt=0
      if [[ -s "${attempt_file}" ]]; then
        attempt="$(<"${attempt_file}")"
      fi
      attempt=$((attempt + 1))
      printf '%s\n' "${attempt}" >"${attempt_file}"
      next_snapshot="${base_snapshot}_retry_$(printf '%02d' "${attempt}")"
      echo "snapshot supervisor: ${current_snapshot} is ${state}; starting ${next_snapshot}"
      create_response="$(curl -fsS -X PUT -H 'Content-Type: application/json' \
        "${es_url}/_snapshot/${repo}/${next_snapshot}?wait_for_completion=false" \
        -d "{\"indices\":\"${index}\",\"include_global_state\":false,\"feature_states\":[],\"partial\":false,\"metadata\":{\"purpose\":\"ningxia_active_index_migration_retry\",\"attempt\":${attempt}}}" \
        2>/dev/null || true)"
      if [[ "$(jq -r '.accepted // false' <<<"${create_response}" 2>/dev/null)" == "true" ]]; then
        current_snapshot="${next_snapshot}"
        printf '%s\n' "${current_snapshot}" >"${current_file}"
      else
        echo "snapshot supervisor: could not start ${next_snapshot}; retrying later"
        sleep 30
      fi
      ;;
    *)
      echo "snapshot supervisor: repository or Elasticsearch temporarily unavailable"
      sleep 30
      ;;
  esac
done
