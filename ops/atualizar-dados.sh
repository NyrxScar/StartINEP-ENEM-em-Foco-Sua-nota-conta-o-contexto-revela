#!/usr/bin/env bash
# =============================================================================
# atualizar-dados.sh — o ciclo de atualizacao automatica do Radar ENEM
#
# Pensado para rodar DENTRO de um Azure Container Apps Job agendado (ver
# ops/provisionar-job.sh), mas tambem roda na mao a partir da raiz do repo.
#
# O que faz, em ordem:
#   1. radar-etl atualizar-auto  -> checa o INEP (ano corrente + anterior) e
#      ingere so o que mudou. Imprime PRATA_MUDOU=sim|nao e usa o codigo de saida
#      para dizer se houve erro (1), se falta contrato (3) ou se esta tudo ok (0).
#   2. Se e so se a Prata mudou, rebuilda a imagem radar-api-comdados (dados
#      embutidos) VIA ACR TASKS (az acr build) — sem Docker local — e atualiza o
#      Container App para a nova tag.
#   3. Se 'atualizar-auto' sinalizou CONTRATO_AUSENTE (saida 3), gera um rascunho
#      de contrato e termina pedindo revisao humana, sem publicar nada.
#
# POR QUE ACR BUILD (e nao docker build): um Container Apps Job nao tem daemon
# Docker. 'az acr build' envia o contexto para o ACR e constroi la dentro. O
# contexto de build do radar-api-comdados precisa conter data/silver (recem
# atualizada) e os dois Dockerfiles — por isso o contexto e a raiz do repo.
#
# Idempotente de proposito: numa semana sem novidade do INEP, o passo 1 diz
# PRATA_MUDOU=nao e o script termina em 0 sem tocar no ACR nem no Container App.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."  # raiz do repositorio

# --- Flags -------------------------------------------------------------------
# --sem-deploy: roda so a ingestao/deteccao (passo 1) e para. Util em dry-run e
# em CI, onde nao se quer tocar no Azure.
SEM_DEPLOY=0
for arg in "$@"; do
  case "$arg" in
    --sem-deploy) SEM_DEPLOY=1 ;;
    *) echo "Flag desconhecida: $arg" >&2; exit 2 ;;
  esac
done

log() { echo "[atualizar-dados] $*"; }

# --- Variaveis do Azure ------------------------------------------------------
# No Job, estas vem do ambiente/segredos (ver provisionar-job.sh). Rodando na
# mao, caem no .azure-deploy-vars.sh do repo.
if [ -z "${RG:-}" ] && [ -f .azure-deploy-vars.sh ]; then
  # shellcheck disable=SC1091
  source .azure-deploy-vars.sh
fi

# --- Passo 1: ingestao/deteccao ---------------------------------------------
# Captura a saida para ler PRATA_MUDOU e, ao mesmo tempo, ecoa para o log do Job.
log "Checando o INEP e ingerindo o que mudou (ano corrente + anterior)..."
set +e
SAIDA=$(cd pipeline && radar-etl atualizar-auto --raiz ../data 2>&1)
COD=$?
set -e
echo "$SAIDA"

PRATA_MUDOU=$(echo "$SAIDA" | sed -n 's/^PRATA_MUDOU=//p' | tail -1)

# Codigo 3 = alguma edicao na origem sem contrato valido: acao humana.
if [ "$COD" -eq 3 ]; then
  log "Uma edicao precisa de contrato. Gerando rascunho(s) para revisao..."
  # Extrai os anos marcados como contrato ausente da propria saida.
  ANOS=$(echo "$SAIDA" | sed -n 's/.*\[\([0-9]\{4\}\)\] contrato_ausente.*/\1/p')
  for ano in $ANOS; do
    log "rascunho-contrato --edicao $ano"
    (cd pipeline && radar-etl rascunho-contrato --edicao "$ano" --raiz ../data) || \
      log "AVISO: nao consegui gerar rascunho para $ano"
  done
  log "Rascunho(s) gerado(s). Nenhum deploy feito: revise o(s) contrato(s) e reexecute."
  exit 3
fi

# Qualquer outro codigo != 0 e erro de verdade.
if [ "$COD" -ne 0 ]; then
  log "ERRO: 'atualizar-auto' terminou com codigo $COD. Nada sera publicado."
  exit "$COD"
fi

if [ "$PRATA_MUDOU" != "sim" ]; then
  log "Nada novo na origem: a Prata nao mudou. Encerrando sem rebuild (idempotente)."
  exit 0
fi

log "A Prata mudou. Seguindo para rebuild + deploy."

if [ "$SEM_DEPLOY" -eq 1 ]; then
  log "--sem-deploy ativo: pulando rebuild/deploy (a Prata local ja foi atualizada)."
  exit 0
fi

# --- Passo 2: rebuild via ACR Tasks + update do Container App -----------------
command -v az >/dev/null || { log "ERRO: Azure CLI (az) nao encontrado."; exit 1; }
: "${RG:?RG nao definido}"; : "${ACR:?ACR nao definido}"; : "${API_APP:?API_APP nao definido}"

ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)

# Proxima tag vN olhando as tags ja existentes do repositorio (mesma regra do deploy.sh).
proxima_versao() {
  local repo="$1" maior
  maior=$(az acr repository show-tags -n "$ACR" --repository "$repo" -o tsv 2>/dev/null \
          | grep -E '^v[0-9]+$' | sed 's/^v//' | sort -n | tail -1 || true)
  if [ -z "$maior" ]; then echo "v1"; else echo "v$((maior + 1))"; fi
}

VER=$(proxima_versao radar-api-comdados)
log "Nova versao: $VER"

# A imagem com dados depende da imagem base. Construimos as duas no ACR, na mesma
# versao, para manter a correspondencia 1:1 entre base e com-dados (igual deploy.sh).
log "ACR build da imagem base (api/Dockerfile)..."
az acr build -r "$ACR" -t "radar-api:$VER" -f api/Dockerfile api/

log "ACR build da imagem com dados embutidos (api/Dockerfile.comdados, contexto = raiz)..."
az acr build -r "$ACR" -t "radar-api-comdados:$VER" \
  --build-arg BASE_IMAGE="$ACR_SERVER/radar-api:$VER" \
  -f api/Dockerfile.comdados .

log "Atualizando Container App '$API_APP' para $VER..."
az containerapp update -n "$API_APP" -g "$RG" \
  --image "$ACR_SERVER/radar-api-comdados:$VER" \
  --query "properties.provisioningState" -o tsv

log "Pronto. API publicada com os dados novos (tag $VER)."

# --- Passo 3: fumaca ---------------------------------------------------------
if [ -n "${API_URL:-}" ]; then
  log "Testando $API_URL/health ..."
  for i in $(seq 1 10); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$API_URL/health" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then log "API respondeu 200."; break; fi
    log "[$i] HTTP $code (aguardando nova revisao)..."; sleep 8
  done
fi
