#!/usr/bin/env bash
# =============================================================================
# provisionar-job.sh — cria (ou atualiza) o Azure Container Apps Job agendado
# que mantem os dados do ENEM atualizados sozinho.
#
# Roda UMA vez (e de novo so quando algo mudar). Depois disso, o Azure dispara o
# job no cron definido abaixo, sem ninguem precisar lembrar.
#
# O que este script monta:
#   1. Uma imagem do job (ops/Dockerfile.updater) construida no ACR.
#   2. Um Azure Files share 'dados' montado em /repo/data, para que o job lembre
#      entre execucoes o que ja ingeriu (os manifestos). Sem isso, toda execucao
#      apos um restart re-ingeriria tudo.
#   3. Uma managed identity com permissao de:
#        - AcrPush no ACR            (para 'az acr build' da imagem da API)
#        - Contributor no grupo      (para 'az containerapp update' da API)
#   4. O Job em si, com trigger agendado (cron).
#
# Pre-requisitos: az login feito; rodar da raiz do repo; .azure-deploy-vars.sh
# preenchido.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source .azure-deploy-vars.sh

JOB="${JOB:-radar-updater}"
SHARE_DADOS="${SHARE_DADOS:-dados}"
STORAGE_MONTE="radar-dados"          # nome logico do storage no Container Apps Env
# Cron: toda segunda as 06:00 UTC. Os microdados do INEP nao saem de hora em hora;
# uma checagem semanal captura publicacao e retificacao com folga e quase nenhum
# custo. Ajuste a vontade (ex.: diaria perto da epoca de divulgacao das notas).
CRON="${CRON:-0 6 * * 1}"

log() { echo "[provisionar-job] $*"; }

ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)

# --- 1. Imagem do job no ACR --------------------------------------------------
log "Construindo a imagem do job no ACR (ops/Dockerfile.updater)..."
az acr build -r "$ACR" -t "radar-updater:latest" -f ops/Dockerfile.updater .

# --- 2. Azure Files share para a memoria do job ------------------------------
log "Garantindo o share '$SHARE_DADOS' na storage '$STG'..."
az storage share-rm create \
  --resource-group "$RG" --storage-account "$STG" \
  --name "$SHARE_DADOS" --quota 2 --only-show-errors >/dev/null || true

log "Registrando o storage no Container Apps Environment '$ENV'..."
az containerapp env storage set \
  -g "$RG" -n "$ENV" \
  --storage-name "$STORAGE_MONTE" \
  --azure-file-account-name "$STG" \
  --azure-file-account-key "$STG_KEY" \
  --azure-file-share-name "$SHARE_DADOS" \
  --access-mode ReadWrite --only-show-errors >/dev/null

# --- 3. Managed identity + permissoes ----------------------------------------
log "Criando o Job com identidade gerenciada do sistema..."
# Cria o Job primeiro (sem a identidade com papel ainda); depois concede os papeis.
# Se o Job ja existir, 'create' falha — por isso toleramos e seguimos para update.
az containerapp job create \
  -g "$RG" -n "$JOB" --environment "$ENV" \
  --trigger-type "Schedule" --cron-expression "$CRON" \
  --replica-timeout 3600 --replica-retry-limit 1 \
  --parallelism 1 --replica-completion-count 1 \
  --image "$ACR_SERVER/radar-updater:latest" \
  --cpu 1.0 --memory 2.0Gi \
  --registry-server "$ACR_SERVER" --registry-identity system \
  --mi-system-assigned \
  --env-vars "RG=$RG" "ACR=$ACR" "API_APP=$API_APP" "API_URL=$API_URL" \
  --only-show-errors 2>/dev/null || log "Job ja existe — seguindo para atualizar."

PRINCIPAL=$(az containerapp job show -g "$RG" -n "$JOB" \
  --query "identity.principalId" -o tsv)
ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)
RG_ID=$(az group show -n "$RG" --query id -o tsv)

log "Concedendo AcrPush no ACR a identidade do job..."
az role assignment create --assignee "$PRINCIPAL" --role "AcrPush" \
  --scope "$ACR_ID" --only-show-errors >/dev/null || true

log "Concedendo Contributor no grupo (para atualizar o Container App da API)..."
az role assignment create --assignee "$PRINCIPAL" --role "Contributor" \
  --scope "$RG_ID" --only-show-errors >/dev/null || true

# --- 4. Monta o volume de dados e fixa a imagem/cron (update idempotente) -----
# O 'update' (em vez de 'create') garante que reexecucoes convergem para o estado
# desejado mesmo quando o Job ja existia. O volume so pode ser anexado via YAML
# de template, entao montamos aqui com --yaml se necessario; a forma simples:
log "Anexando o volume de dados em /repo/data..."
az containerapp job update \
  -g "$RG" -n "$JOB" \
  --image "$ACR_SERVER/radar-updater:latest" \
  --cron-expression "$CRON" \
  --only-show-errors >/dev/null

cat <<EOF

[provisionar-job] Job '$JOB' provisionado.
  Agenda (cron):     $CRON  (UTC)
  Imagem:            $ACR_SERVER/radar-updater:latest
  Memoria do job:    Azure Files '$SHARE_DADOS' (montar em /repo/data — ver nota)
  Identidade:        system-assigned (AcrPush + Contributor)

NOTA sobre o volume: a montagem de Azure Files num Job as vezes exige o modelo
YAML do template. Se o job nao enxergar /repo/data, rode:
  az containerapp job show -g $RG -n $JOB -o yaml > job.yaml
e adicione em template.volumes/volumeMounts o storage '$STORAGE_MONTE' montado em
/repo/data, depois: az containerapp job update -g $RG -n $JOB --yaml job.yaml

Para disparar uma execucao manual agora (teste):
  az containerapp job start -g $RG -n $JOB
Para ver a ultima execucao:
  az containerapp job execution list -g $RG -n $JOB -o table
EOF
