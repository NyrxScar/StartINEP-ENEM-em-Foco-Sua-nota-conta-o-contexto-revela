# Atualização automática dos dados do ENEM (Opção 2)

Fluxo que mantém os dados em dia com o INEP **sozinho**, via GitHub Actions,
**sem precisar logar no Azure** (o plano Azure for Students não permite criar
service principal — sua conta é Owner da assinatura, mas não tem acesso ao
diretório/Entra ID para registrar aplicativos).

Arquivo: `.github/workflows/atualizar-dados-enem.yml`.

## Como funciona — divisão de trabalho

**O robô (GitHub Actions, automático, grátis)** toda segunda-feira:
1. Checa o INEP (ano atual + anterior) e ingere só o que mudou.
2. Se a silver mudou: builda a imagem da API com os dados e **dá push no
   registry (ACR)** — autenticando só no Docker, com a senha do ACR.
3. Commita a silver atualizada (memória entre execuções).
4. **Abre uma issue** avisando: "imagem nova pronta, publique".

**Você (um comando, quando vir o aviso):**
```bash
./deploy.sh api
```
Esse passo precisa de `az login` (só você tem). O `deploy.sh` publica a imagem
nova no Container App.

> **Por que esse passo é seu e não do robô:** `az containerapp update` exige
> autenticação no Azure, e qualquer login não-interativo precisaria de um app
> registrado no diretório — exatamente o que sua instituição bloqueia. O push da
> imagem, por outro lado, usa só a senha do registry e roda sem problema.

**Ano novo com estrutura diferente** (o INEP muda arquivo/colunas entre edições)
→ o robô gera um contrato-rascunho e **abre um Pull Request** pedindo revisão.
Você ajusta o mapeamento uma vez, dá merge, e a partir daí aquele ano entra
sozinho. Esse passo humano não pode ser removido com segurança.

A escolha de anos é automática (ano atual + anterior): em 2026 olha 2026/2025,
em 2027 olha 2027/2026, etc. Nada para manter a cada ano.

---

## Setup (uma vez) — cadastrar 3 secrets no GitHub

Não precisa de service principal. Só das credenciais do registry (que já
existem, porque o ACR foi criado com admin habilitado).

No repositório `NyrxScar/StartINEP---...`, vá em
**Settings → Secrets and variables → Actions → New repository secret** e crie:

| Nome do secret | Valor |
|----------------|-------|
| `ACR_LOGIN_SERVER` | `radarenem2026.azurecr.io` |
| `ACR_USERNAME` | `radarenem2026` |
| `ACR_PASSWORD` | *(a senha do ACR — pegue com o comando abaixo)* |

Para ver a senha do ACR a qualquer momento:
```bash
source .azure-deploy-vars.sh
az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv
```

> **Segurança:** se um dia quiser rotacionar essa senha, rode
> `az acr credential renew -n radarenem2026 --password-name password` e atualize
> o secret `ACR_PASSWORD` no GitHub.

### Permissões do workflow
Em **Settings → Actions → General → Workflow permissions**, marque
**"Read and write permissions"** (o robô commita a silver, abre issue e PR).

### Dados no git
O workflow commita a silver (~167 MB) com `git add -f` para lembrar o que já
ingeriu entre execuções (hoje `data/` está no `.gitignore`). Para um projeto
acadêmico, tudo bem. Se preferir Git LFS ou storage externo, dá para mudar
depois.

---

## Testar sem esperar a segunda-feira

Aba **Actions** → workflow **"Atualização automática dos dados do ENEM"** →
**"Run workflow"**. Se não houver novidade no INEP, encerra rápido (esperado).

Para testar a detecção localmente:
```bash
cd pipeline
radar-etl atualizar-auto --raiz ../data
```

---

## Resumo do fluxo

```
Segunda 06:00 UTC ─► radar-etl atualizar-auto (ano atual + anterior)
                        │
      ┌──────────────────┼───────────────────────────┐
      ▼                  ▼                             ▼
  nada mudou        dado novo (formato              ano novo (formato
  (encerra)         conhecido)                      diferente)
                        │                             │
                        ▼                             ▼
             build + PUSH no ACR               gera rascunho +
             commit da silver                  abre Pull Request
             abre issue "publicar"             (revisão humana)
                        │
                        ▼
             VOCÊ: ./deploy.sh api
```
