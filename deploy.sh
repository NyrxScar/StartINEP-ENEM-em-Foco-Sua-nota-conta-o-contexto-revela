#!/usr/bin/env bash
# =============================================================================
# deploy.sh — atualiza o Radar ENEM no Azure (Container Apps)
#
# Faz o ciclo completo: build -> push -> update -> teste.
# Descobre a próxima versão sozinho (lê as tags já existentes no ACR) e loga
# no registry automaticamente.
#
# USO:
#   ./deploy.sh api      # rebuilda e atualiza só o backend (API + dados)
#   ./deploy.sh web      # rebuilda e atualiza só o frontend (site)
#   ./deploy.sh all      # os dois
#   ./deploy.sh status   # só mostra o que está no ar (não muda nada)
#
# Pré-requisitos: az login feito, Docker rodando, estar na raiz do projeto.
# =============================================================================
set -euo pipefail

# --- Diretório do script = raiz do projeto -----------------------------------
cd "$(dirname "$0")"

# --- Cores para legibilidade -------------------------------------------------
if [ -t 1 ]; then
  AZUL='\033[0;34m'; VERDE='\033[0;32m'; AMARELO='\033[0;33m'; VERMELHO='\033[0;31m'; FIM='\033[0m'
else
  AZUL=''; VERDE=''; AMARELO=''; VERMELHO=''; FIM=''
fi
info()  { echo -e "${AZUL}==>${FIM} $*"; }
ok()    { echo -e "${VERDE}✓${FIM} $*"; }
aviso() { echo -e "${AMARELO}!${FIM} $*"; }
erro()  { echo -e "${VERMELHO}✗ $*${FIM}" >&2; }

# --- Carregar variáveis do deploy --------------------------------------------
if [ ! -f .azure-deploy-vars.sh ]; then
  erro "Arquivo .azure-deploy-vars.sh não encontrado. Rode a partir da raiz do projeto."
  exit 1
fi
# shellcheck disable=SC1091
source .azure-deploy-vars.sh

# --- Checar comandos ---------------------------------------------------------
command -v az >/dev/null     || { erro "Azure CLI (az) não encontrado."; exit 1; }
command -v docker >/dev/null || { erro "Docker não encontrado."; exit 1; }

# --- Config isolada do Docker (evita o credsStore 'pass' quebrado) -----------
export DOCKER_CONFIG="$PWD/.docker-deploy"
mkdir -p "$DOCKER_CONFIG"
[ -f "$DOCKER_CONFIG/config.json" ] || echo '{}' > "$DOCKER_CONFIG/config.json"

# --- Resolver dados do ACR ---------------------------------------------------
resolver_acr() {
  ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
  ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
  ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)
}

login_docker() {
  info "Login no registry $ACR_SERVER..."
  echo "$ACR_PASS" | docker login "$ACR_SERVER" --username "$ACR_USER" --password-stdin >/dev/null
  ok "Docker logado no ACR."
}

# Descobre a próxima tag vN+1 olhando as tags existentes do repositório.
proxima_versao() {
  local repo="$1"
  local maior
  maior=$(az acr repository show-tags -n "$ACR" --repository "$repo" -o tsv 2>/dev/null \
          | grep -E '^v[0-9]+$' | sed 's/^v//' | sort -n | tail -1 || true)
  if [ -z "$maior" ]; then echo "v1"; else echo "v$((maior + 1))"; fi
}

testar_api() {
  local url="$API_URL/health"
  info "Testando API ($url)..."
  for i in $(seq 1 10); do
    local code; code=$(curl -s -o /tmp/_api_health -w '%{http_code}' --max-time 30 "$url" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then ok "API respondeu 200: $(cat /tmp/_api_health)"; return 0; fi
    aviso "[$i] HTTP $code (aguardando nova revisão / cold start)..."; sleep 8
  done
  erro "API não respondeu 200 a tempo. Veja os logs: az containerapp logs show -n $API_APP -g $RG --tail 50"
  return 1
}

testar_web() {
  info "Testando site ($WEB_URL)..."
  for i in $(seq 1 10); do
    local code; code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$WEB_URL/" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then ok "Site respondeu 200."; return 0; fi
    aviso "[$i] HTTP $code (aguardando nova revisão / cold start)..."; sleep 8
  done
  erro "Site não respondeu 200 a tempo."
  return 1
}

# --- Deploy do BACKEND -------------------------------------------------------
deploy_api() {
  local ver; ver=$(proxima_versao radar-api-comdados)
  info "Backend: nova versão ${ver}"

  info "Build da imagem base (api/Dockerfile)..."
  docker build -t "$ACR_SERVER/radar-api:$ver" -f api/Dockerfile api/

  info "Build da imagem com dados embutidos (api/Dockerfile.comdados)..."
  docker build --build-arg BASE_IMAGE="$ACR_SERVER/radar-api:$ver" \
    -t "$ACR_SERVER/radar-api-comdados:$ver" -f api/Dockerfile.comdados .

  info "Push..."
  docker push "$ACR_SERVER/radar-api-comdados:$ver"

  info "Atualizando Container App '$API_APP'..."
  az containerapp update -n "$API_APP" -g "$RG" \
    --image "$ACR_SERVER/radar-api-comdados:$ver" --query "properties.provisioningState" -o tsv

  ok "Backend atualizado para $ver."
  testar_api || true
}

# --- Deploy do FRONTEND ------------------------------------------------------
deploy_web() {
  local ver; ver=$(proxima_versao radar-web)
  info "Frontend: nova versão ${ver}"
  info "VITE_API_URL embutida: $API_URL"

  info "Build do site (web/Dockerfile)..."
  docker build --build-arg VITE_API_URL="$API_URL" \
    -t "$ACR_SERVER/radar-web:$ver" -f web/Dockerfile web/

  info "Push..."
  docker push "$ACR_SERVER/radar-web:$ver"

  info "Atualizando Container App '$WEB_APP'..."
  az containerapp update -n "$WEB_APP" -g "$RG" \
    --image "$ACR_SERVER/radar-web:$ver" --query "properties.provisioningState" -o tsv

  ok "Frontend atualizado para $ver."
  testar_web || true
}

# --- Status (não muda nada) --------------------------------------------------
mostrar_status() {
  info "Imagem atual da API:"
  az containerapp show -n "$API_APP" -g "$RG" --query "properties.template.containers[0].image" -o tsv
  info "Imagem atual do site:"
  az containerapp show -n "$WEB_APP" -g "$RG" --query "properties.template.containers[0].image" -o tsv
  echo
  info "Links:"
  echo "  Site: $WEB_URL"
  echo "  API:  $API_URL"
  echo
  testar_api || true
  testar_web || true
}

# --- Main --------------------------------------------------------------------
ALVO="${1:-}"
case "$ALVO" in
  api)    resolver_acr; login_docker; deploy_api ;;
  web)    resolver_acr; login_docker; deploy_web ;;
  all)    resolver_acr; login_docker; deploy_api; deploy_web ;;
  status) resolver_acr; mostrar_status ;;
  *)
    echo "Uso: ./deploy.sh {api|web|all|status}"
    echo
    echo "  api     rebuilda e atualiza o backend (API + dados silver)"
    echo "  web     rebuilda e atualiza o frontend (site)"
    echo "  all     atualiza os dois"
    echo "  status  mostra as imagens no ar e testa os endpoints (não muda nada)"
    exit 1
    ;;
esac

echo
ok "Pronto."
echo "   Site: $WEB_URL"
echo "   API:  $API_URL"
