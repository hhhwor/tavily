#!/usr/bin/env bash
set -uo pipefail

repo="ningxia_migration"
index="openalex_full_20260706"
snapshot_prefix="openalex_full_20260706_ningxia_20260807"
expected_docs="510372821"
state_dir="/var/lib/openalex-migration"
restore_file="${state_dir}/restore_snapshot"
success_file="${state_dir}/restore_complete"
es_url="http://127.0.0.1:9200"

mkdir -p "${state_dir}"

while true; do
  if [[ -s "${restore_file}" ]]; then
    snapshot="$(<"${restore_file}")"
  else
    response="$(curl -fsS --retry 3 --retry-delay 5 --max-time 30 \
      "${es_url}/_snapshot/${repo}/_all?verbose=false" 2>/dev/null || true)"
    snapshot="$(jq -r --arg prefix "${snapshot_prefix}" \
      '[.snapshots[]? | select(.state == "SUCCESS") | select(.snapshot | startswith($prefix))] | sort_by(.end_time_in_millis) | last | .snapshot // empty' \
      <<<"${response}" 2>/dev/null || true)"
    if [[ -z "${snapshot}" ]]; then
      sleep 30
      continue
    fi

    if ! curl -fsS --max-time 20 "${es_url}/${index}" >/dev/null 2>&1; then
      echo "restore supervisor: restoring ${snapshot}"
      restore_response="$(curl -fsS -X POST -H 'Content-Type: application/json' \
        "${es_url}/_snapshot/${repo}/${snapshot}/_restore?wait_for_completion=false" \
        -d "{\"indices\":\"${index}\",\"include_global_state\":false,\"index_settings\":{\"index.number_of_replicas\":0}}" \
        2>/dev/null || true)"
      if [[ "$(jq -r '.accepted // false' <<<"${restore_response}" 2>/dev/null)" != "true" ]]; then
        echo "restore supervisor: restore request was not accepted; retrying later"
        sleep 30
        continue
      fi
    fi
    printf '%s\n' "${snapshot}" >"${restore_file}"
  fi

  health="$(curl -fsS --max-time 30 "${es_url}/_cluster/health/${index}" 2>/dev/null | jq -r '.status // empty' 2>/dev/null || true)"
  if [[ "${health}" != "green" ]]; then
    sleep 30
    continue
  fi

  docs="$(curl -fsS --max-time 30 "${es_url}/${index}/_count" 2>/dev/null | jq -r '.count // 0' 2>/dev/null || true)"
  if [[ "${docs}" != "${expected_docs}" ]]; then
    echo "restore supervisor: green index has unexpected count ${docs}; expected ${expected_docs}"
    sleep 30
    continue
  fi

  alias_index="$(curl -fsS --max-time 30 "${es_url}/_alias/openalex_read" 2>/dev/null | jq -r 'keys[0] // empty' 2>/dev/null || true)"
  if [[ -z "${alias_index}" ]]; then
    curl -fsS -X PUT "${es_url}/${index}/_alias/openalex_read" >/dev/null
    alias_index="${index}"
  fi
  if [[ "${alias_index}" != "${index}" ]]; then
    echo "restore supervisor: openalex_read points to unexpected index ${alias_index}"
    exit 1
  fi

  printf 'snapshot=%s\nindex=%s\ndocs=%s\n' "${snapshot}" "${index}" "${docs}" >"${success_file}"
  echo "restore supervisor: ${index} restored and verified with ${docs} documents"
  exit 0
done
