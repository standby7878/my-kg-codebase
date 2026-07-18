#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <B|M|MF> <target-checkout> <prompt-file> <log-dir>" >&2
  exit 2
}

[[ $# -eq 4 ]] || usage
condition="$1"
target="$2"
prompt_file="$3"
log_dir="$4"
case "$condition" in B|M|MF) ;; *) echo "invalid condition: $condition" >&2; exit 2 ;; esac

[[ -d "$target" ]] || { echo "target checkout is not a directory: $target" >&2; exit 1; }
[[ -f "$prompt_file" && -r "$prompt_file" ]] || { echo "prompt file is not a readable regular file: $prompt_file" >&2; exit 1; }

target_abs="$(cd "$target" && pwd -P)"
prompt_abs="$(cd "$(dirname "$prompt_file")" && pwd -P)/$(basename "$prompt_file")"
log_abs="$(realpath -m -- "$log_dir")"
case "$target_abs" in /|/tmp|/var/tmp) echo "refusing unsafe target checkout path: $target_abs" >&2; exit 1 ;; esac

source_root="$(git -C "$target_abs" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "refusing non-checkout target: $target" >&2
  exit 1
}
source_root="$(cd "$source_root" && pwd -P)"
[[ "$source_root" == "$target_abs" ]] || {
  echo "target must be the checkout root: $target_abs" >&2
  exit 1
}
[[ "$(git -C "$source_root" status --porcelain=v1 --untracked-files=all)" == "" ]] || {
  echo "refusing dirty source checkout: $source_root" >&2
  exit 1
}
source_commit="$(git -C "$source_root" rev-parse --verify HEAD^{commit} 2>/dev/null)" || {
  echo "refusing source checkout without a valid HEAD: $source_root" >&2
  exit 1
}
case "$log_abs" in
  /)
    echo "refusing unsafe log directory path: $log_abs" >&2
    exit 1
    ;;
  "$source_root"|"$source_root"/*)
    echo "refusing log directory inside source checkout: $log_abs" >&2
    exit 1
    ;;
esac
mkdir -p "$log_abs"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
benchmark_dir="$(cd "$script_dir/.." && pwd -P)"
config_dir="$log_abs/config-$condition"
mkdir -p "$config_dir"
output="$log_abs/${condition}.jsonl"
prompt_text="$(< "$prompt_abs")"
if [[ "$condition" == MF ]]; then
  prompt_text+=$'\n\nUse the CodeKG MCP first when available.'
fi

args=(--config-dir "$config_dir" --model gpt-4.1 --disable-builtin-mcps --no-auto-update --allow-all-tools --no-ask-user --output-format json --log-dir "$log_abs")
if [[ "$condition" != B ]]; then
  args+=(--additional-mcp-config "@$benchmark_dir/config/codekg-mcp.json")
fi

command -v copilot >/dev/null 2>&1 || { echo "Copilot CLI not found" >&2; exit 1; }
[[ "$(git -C "$source_root" rev-parse --verify HEAD^{commit})" == "$source_commit" ]] || {
  echo "source HEAD changed during validation: $source_root" >&2
  exit 1
}
[[ "$(git -C "$source_root" status --porcelain=v1 --untracked-files=all)" == "" ]] || {
  echo "source checkout became dirty during validation: $source_root" >&2
  exit 1
}

clone_dir="$(mktemp -d "$log_abs/.copilot-source-${condition}.XXXXXX")"
clone_dir="$(cd "$clone_dir" && pwd -P)"
cleanup_clone() {
  if [[ -n "${clone_dir:-}" && -d "$clone_dir" && "$clone_dir" != "$log_abs" && "$clone_dir" == "$log_abs"/* ]]; then
    rm -rf -- "$clone_dir"
  fi
}
trap cleanup_clone EXIT

git clone --no-local --no-hardlinks -- "$source_root" "$clone_dir" >/dev/null
git -C "$clone_dir" checkout --detach "$source_commit" >/dev/null
[[ "$(git -C "$clone_dir" rev-parse --verify HEAD^{commit})" == "$source_commit" ]] || {
  echo "temporary clone did not reach the pinned source commit" >&2
  exit 1
}
[[ "$(git -C "$clone_dir" symbolic-ref --quiet -q HEAD 2>/dev/null || true)" == "" ]] || {
  echo "temporary clone HEAD is not detached" >&2
  exit 1
}

cd "$clone_dir"
command -v copilot >/dev/null 2>&1 || { echo "Copilot CLI not found" >&2; exit 1; }
echo "Run only after corpus/truth sign-off; confirm gpt-4.1 entitlement/version and use the disposable temporary clone." >&2
copilot "${args[@]}" -p "$prompt_text" > "$output"
