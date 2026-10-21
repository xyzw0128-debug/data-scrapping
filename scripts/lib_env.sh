# Shared shell helpers for run_daily.sh and rclone_backup.sh.

# Export KEY=VALUE lines from a dotenv file without overriding variables that are already
# set, with the same rules as src/env.py (comments, optional "export ", matching quotes).
load_dotenv() {
  local file="$1" line key value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 && "${value:0:1}" == "${value: -1}" && ( "${value:0:1}" == '"' || "${value:0:1}" == "'" ) ]]; then
      value="${value:1:${#value}-2}"
    fi
    if [[ -z "${!key+x}" ]]; then
      export "$key=$value"
    fi
  done < "$file"
}

# Take the pipeline lock (data/state/pipeline.lock) on file descriptor 9 unless the caller
# already runs under it. The kernel releases it when the process tree holding fd 9 exits.
acquire_pipeline_lock() {
  local root="$1" wait_seconds="${2:-30}" lock_file
  if [[ "${DATA_SCRAPPING_PIPELINE_LOCK:-}" == "1" ]]; then
    return 0
  fi
  lock_file="$root/data/state/pipeline.lock"
  mkdir -p "$(dirname "$lock_file")"
  exec 9<>"$lock_file"
  if ! flock -w "$wait_seconds" 9; then
    printf 'Pipeline lock is held at %s (%s); exiting\n' "$lock_file" "$(tr '\n' ' ' < "$lock_file")" >&2
    return 2
  fi
  printf 'pid=%s\nholder=%s\n' "$$" "$(basename "$0")" > "$lock_file"
  export DATA_SCRAPPING_PIPELINE_LOCK=1
}
