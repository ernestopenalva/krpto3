#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"

cd "${PROJECT_ROOT}"

echo "=================================================="
echo "Limpando ambiente KRPTO3"
echo "=================================================="
echo

required_files=(
  "src/app.py"
  "src/modules/token_scanner.py"
  "config/config.yaml"
)

for required_file in "${required_files[@]}"; do
  if [[ ! -f "${PROJECT_ROOT}/${required_file}" ]]; then
    echo "ERRO: este diretorio nao parece ser a raiz do projeto KRPTO3."
    echo "Arquivo obrigatorio nao encontrado: ${required_file}"
    exit 1
  fi
done

runtime_paths=(
  "logs"
  "data/token_scanner"
  "data/token_monitor"
  "data/position_monitor"
  "data/watchlist"
  "data/snapshots"
)

echo "Os seguintes diretorios/arquivos de runtime serao limpos:"
for runtime_path in "${runtime_paths[@]}"; do
  echo "  - ${runtime_path}/"
done
echo

read -r -p "Para confirmar, digite exatamente LIMPAR: " confirmation

if [[ "${confirmation}" != "LIMPAR" ]]; then
  echo "Operacao abortada. Nada foi apagado."
  exit 0
fi

ensure_inside_project() {
  local target="$1"
  local parent
  local absolute_parent
  local absolute_target

  parent="$(dirname "${target}")"
  mkdir -p "${parent}"

  absolute_parent="$(cd "${parent}" && pwd -P)"
  absolute_target="${absolute_parent}/$(basename "${target}")"

  case "${absolute_target}" in
    "${PROJECT_ROOT}"/*) ;;
    *)
      echo "ERRO: caminho fora da raiz do projeto: ${target}"
      exit 1
      ;;
  esac
}

clear_runtime_dir() {
  local relative_dir="$1"
  local absolute_dir="${PROJECT_ROOT}/${relative_dir}"

  if [[ -z "${relative_dir}" || "${relative_dir}" == "/" || "${relative_dir}" == "." ]]; then
    echo "ERRO: caminho invalido para limpeza: ${relative_dir}"
    exit 1
  fi

  ensure_inside_project "${absolute_dir}"
  mkdir -p "${absolute_dir}"

  find "${absolute_dir}" -mindepth 1 -exec rm -rf -- {} +
}

for runtime_path in "${runtime_paths[@]}"; do
  clear_runtime_dir "${runtime_path}"
done

mkdir -p \
  "logs" \
  "data/token_scanner" \
  "data/token_monitor" \
  "data/position_monitor/history" \
  "data/watchlist" \
  "data/snapshots"

cat > "data/watchlist/watchlist.json" <<'JSON'
{}
JSON

cat > "data/token_scanner/final_monitoring_candidates.json" <<'JSON'
{
  "generated_at": null,
  "scanner_version": null,
  "total_candidates": 0,
  "candidates": []
}
JSON

echo
echo "Ambiente limpo com sucesso"
echo
echo "Estrutura recriada:"
find logs data/token_scanner data/token_monitor data/position_monitor data/watchlist data/snapshots -maxdepth 2 -print | sort
