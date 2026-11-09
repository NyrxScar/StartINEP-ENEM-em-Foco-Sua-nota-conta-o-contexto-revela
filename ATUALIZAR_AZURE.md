# Como atualizar o sistema no Azure

Guia prático para quando você mudar o código (ou os dados) e quiser que a
versão no Azure reflita a mudança.

> **O Azure NÃO atualiza sozinho quando você muda o CÓDIGO.** O que roda lá é uma
> *imagem Docker* — uma fotografia do código no momento do build. Editar arquivos
> na sua máquina não muda nada no Azure até você refazer o ciclo de 3 passos:
>
> **1. Rebuildar a imagem → 2. Enviar (push) → 3. Mandar o app usar a nova imagem.**
>
> **Os DADOS do ENEM, porém, atualizam sozinhos.** Quando o INEP publica um ano
> novo ou retifica um existente, um job agendado detecta, ingere e republica a API
> automaticamente. Veja [docs/atualizacao-automatica.md](docs/atualizacao-automatica.md).
> Este guia aqui é só para quando *você* muda o código.

---

## Antes de tudo (uma vez por sessão de terminal)

Abra um terminal na raiz do projeto e carregue as variáveis + login:

```bash
source .azure-deploy-vars.sh
export DOCKER_CONFIG="$PWD/.docker-deploy"

# Garante que o Docker está logado no registry (se já estiver, é rápido)
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)
echo "$ACR_PASS" | docker login "$ACR_SERVER" --username "$ACR_USER" --password-stdin
```

> Se você não estiver logado na Azure CLI, rode antes: `az login`

**Links do sistema (para conferir depois de atualizar):**
- Site: https://radar-web.kindbeach-0c4857e4.southafricanorth.azurecontainerapps.io
- API:  https://radar-api.kindbeach-0c4857e4.southafricanorth.azurecontainerapps.io

---

## Caso A — Mudei o BACKEND (código em `api/`)

A imagem da API tem os dados (silver) embutidos, então são dois builds: a base e
a versão com dados. **Troque o número da versão a cada atualização** (`v3`, `v4`…),
assim fica fácil voltar atrás se algo quebrar.

```bash
source .azure-deploy-vars.sh
export DOCKER_CONFIG="$PWD/.docker-deploy"
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)

VER=v3   # <- aumente a cada deploy (v4, v5, ...)

# 1. Build da imagem base + build com dados embutidos
docker build -t "$ACR_SERVER/radar-api:$VER" -f api/Dockerfile api/
docker build --build-arg BASE_IMAGE="$ACR_SERVER/radar-api:$VER" \
  -t "$ACR_SERVER/radar-api-comdados:$VER" -f api/Dockerfile.comdados .

# 2. Push
docker push "$ACR_SERVER/radar-api-comdados:$VER"

# 3. Mandar o app usar a nova imagem
az containerapp update -n "$API_APP" -g "$RG" \
  --image "$ACR_SERVER/radar-api-comdados:$VER"

# Conferir
curl -s "$API_URL/health"; echo
```

---

## Caso B — Mudei o FRONTEND (código em `web/`)

> **Atenção:** o endereço da API é gravado DENTRO do site no momento do build.
> Por isso o `--build-arg VITE_API_URL=...` é obrigatório — sem ele o site
> apontaria para `localhost` e pararia de funcionar no ar.

```bash
source .azure-deploy-vars.sh
export DOCKER_CONFIG="$PWD/.docker-deploy"
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)

VER=v2   # <- aumente a cada deploy

# 1. Build (com a URL da API embutida)
docker build --build-arg VITE_API_URL="$API_URL" \
  -t "$ACR_SERVER/radar-web:$VER" -f web/Dockerfile web/

# 2. Push
docker push "$ACR_SERVER/radar-web:$VER"

# 3. Mandar o app usar a nova imagem
az containerapp update -n "$WEB_APP" -g "$RG" \
  --image "$ACR_SERVER/radar-web:$VER"

# Conferir
curl -s -o /dev/null -w 'Site: HTTP %{http_code}\n' "$WEB_URL/"
```

---

## Caso C — Mudei os DADOS (`data/silver/`)

Os dados estão embutidos na imagem da API, então é o **mesmo procedimento do
Caso A**: rebuildar `radar-api-comdados` (o passo `COPY data/silver` pega os
arquivos novos) e dar `update`. Não há nada separado para sincronizar.

---

## Caso D — Mudei SÓ configuração (variável de ambiente)

Se a mudança é só uma variável (ex.: trocar o limiar de agregação, CORS, etc.),
**não precisa rebuildar imagem**. Só atualiza a variável:

```bash
source .azure-deploy-vars.sh

# Exemplo: mudar o limiar de agregação
az containerapp update -n "$API_APP" -g "$RG" \
  --set-env-vars RADAR_LIMIAR_AGREGACAO=30

# Exemplo: mudar as origens de CORS (quando o link do site mudar)
az containerapp update -n "$API_APP" -g "$RG" \
  --set-env-vars RADAR_CORS_ORIGENS="$WEB_URL"
```

Variáveis disponíveis na API: `RADAR_SILVER_ROOT`, `RADAR_LIMIAR_AGREGACAO`,
`RADAR_ML_HABILITADO`, `RADAR_TIMEOUT_CONSULTA_S`, `RADAR_AMBIENTE_REFERENCIA`,
`RADAR_CORS_ORIGENS`, `PORT`.

---

## Verificar se a atualização subiu

```bash
source .azure-deploy-vars.sh

# Qual imagem cada app está rodando agora
az containerapp show -n "$API_APP" -g "$RG" \
  --query "properties.template.containers[0].image" -o tsv
az containerapp show -n "$WEB_APP" -g "$RG" \
  --query "properties.template.containers[0].image" -o tsv

# Testes rápidos
curl -s "$API_URL/health"; echo
curl -s -o /dev/null -w 'Site: HTTP %{http_code}\n' "$WEB_URL/"
```

> Depois de um `update`, a nova revisão leva alguns segundos para assumir o
> tráfego. Se o teste ainda mostrar a versão antiga, espere ~30s e repita.

---

## Se algo quebrar: voltar para a versão anterior (rollback)

Como cada deploy usa uma tag de versão (`v2`, `v3`…), voltar atrás é só apontar
o app para a imagem antiga:

```bash
source .azure-deploy-vars.sh
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)

# Ex.: a v3 quebrou, voltar a API para a v2
az containerapp update -n "$API_APP" -g "$RG" \
  --image "$ACR_SERVER/radar-api-comdados:v2"
```

Para ver quais versões existem no registry:

```bash
az acr repository show-tags -n "$ACR" --repository radar-api-comdados -o table
az acr repository show-tags -n "$ACR" --repository radar-web -o table
```

---

## Lembretes importantes

- **Sempre aumente o número da versão** (`v3`, `v4`…) em vez de reusar a mesma
  tag. Isso dá histórico e permite rollback.
- **Frontend sempre com `VITE_API_URL`** no build.
- **Cold start:** os apps estão com `min-replicas 0` (dormem sem acesso). A
  primeira visita depois de um tempo parado demora 10–30s; isso é normal e
  não é erro.
- **Custo:** o ciclo de atualização em si não gasta quase nada; o que pesa é o
  registry (ACR) e apps acordados. Dormindo, o crédito de estudante dura muito.
- As variáveis e a chave do registry estão em `.azure-deploy-vars.sh`
  (não versionar — já está no `.gitignore`).
