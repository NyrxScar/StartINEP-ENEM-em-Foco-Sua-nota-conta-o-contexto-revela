# Atualização automática dos dados do ENEM

Este documento explica como o Radar ENEM se mantém atualizado **sozinho**: quando
o INEP publica as notas de um ano novo (ou retifica um ano já publicado), o sistema
detecta, ingere e republica a API — sem ninguém precisar rodar comando nenhum.

A única exceção, explicada mais abaixo, é quando o INEP muda a *estrutura* do pacote
de dados: aí um humano revisa um rascunho de contrato em poucos minutos.

---

## O que é automático e o que não é

| Situação | O sistema faz sozinho? |
|---|---|
| Saem as notas de um ano que já tem contrato (ex.: 2026 com a mesma estrutura de 2023) | **Sim, 100%.** Detecta, ingere e republica. |
| O INEP **retifica** um ano já publicado (muda o ZIP) | **Sim, 100%.** O tamanho/`Last-Modified` muda, o sistema reingere. |
| Sai um ano com **estrutura nova** (nome de arquivo ou colunas diferentes) | **Quase.** Gera um rascunho de contrato e pede revisão humana (minutos). |
| Um portão de qualidade reprova (ex.: queda abrupta de linhas) | **Não.** Para e avisa — de propósito, para não publicar dado suspeito. |

Por que não é 100% para sempre: o INEP muda o layout interno do pacote de um ano
para o outro. Em 2024 o ZIP trazia `RESULTADOS_2024.csv` (com notas); em 2025, só
`PARTICIPANTES_2025.csv` (sem notas). Decidir o que isso significa — qual arquivo
ingerir, se uma coluna que sumiu é uma lacuna legítima — exige julgamento. O sistema
automatiza todo o trabalho mecânico e isola essa decisão num passo curto e visível.

---

## Como funciona (o ciclo)

Um **Azure Container Apps Job** roda num agendamento (por padrão, toda segunda às
06:00 UTC). Cada execução faz:

1. **Detecta** — para o ano corrente e o anterior, pergunta ao INEP (sem baixar o
   arquivo inteiro) se o ZIP mudou desde a última ingestão, comparando com o
   manifesto guardado. Os dois únicos anos que ainda podem mudar são esses; em 2026
   são 2026 e 2025, em 2027 são 2027 e 2026, e assim por diante, sem lista fixa.
2. **Ingere** — se houve mudança, roda o pipeline (Bronze → portões de qualidade →
   Prata), exatamente o mesmo caminho da ingestão manual. Se nada mudou, para aqui.
3. **Republica** — se a camada *silver* mudou de fato, reconstrói a imagem da API
   (com os dados embutidos) no próprio ACR e atualiza o Container App para a nova
   versão. Se a silver não mudou, não republica nada.

O código vive em três lugares:

- `pipeline/src/radar_etl/auto.py` — a lógica de decisão (anos-alvo, detecção).
- `pipeline/src/radar_etl/cli.py` — os comandos `atualizar-auto` e `rascunho-contrato`.
- `ops/atualizar-dados.sh` — o orquestrador que encadeia ingestão + rebuild + deploy.

---

## Provisionar o job (uma vez)

Com `az login` feito e na raiz do repositório:

```bash
./ops/provisionar-job.sh
```

Isso cria a imagem do job no ACR, um Azure Files share `dados` (a "memória" do job
entre execuções), a identidade gerenciada com as permissões necessárias (AcrPush no
ACR e Contributor no grupo), e o job agendado.

Para mudar a frequência, defina `CRON` antes de rodar (sintaxe cron, em UTC):

```bash
CRON="0 6 * * *" ./ops/provisionar-job.sh   # diária às 06:00 UTC
```

> **Nota sobre o volume de dados:** montar Azure Files num *Job* às vezes exige o
> modelo YAML do template. O próprio `provisionar-job.sh` imprime o passo exato
> caso o job não enxergue `/repo/data`. Sem o volume o sistema ainda funciona, mas
> re-ingere do zero a cada reinício (desperdício, não erro).

---

## Rodar sob demanda

Disparar uma execução agora, sem esperar o cron:

```bash
az containerapp job start -g "$RG" -n radar-updater
```

Ver as execuções e os logs:

```bash
az containerapp job execution list -g "$RG" -n radar-updater -o table
az containerapp job logs show -g "$RG" -n radar-updater --container radar-updater --tail 100
```

---

## Quando um humano precisa entrar (contrato novo)

Se o job encontra um ano com estrutura nova, ele **não publica nada** e termina com
código 3, deixando um rascunho de contrato em
`pipeline/src/radar_etl/contracts/enem_<ANO>.rascunho.yml`.

Para resolver:

1. Abra o rascunho. No topo há um bloco **REVISAR** listando o CSV escolhido e as
   colunas que não casaram automaticamente.
2. Confirme se o arquivo CSV é o certo (o sistema escolhe o maior do ZIP).
3. Para cada coluna em `ausentes` marcada com `REVISAR`, escreva uma justificativa
   real — os portões exigem justificativa não-vazia — ou mova a coluna para
   `mapeamento` se existe uma coluna de origem equivalente com outro nome.
4. Renomeie o arquivo de `enem_<ANO>.rascunho.yml` para `enem_<ANO>.yml`.

Na próxima execução (ou disparando o job na mão), o ano passa a ser ingerido e
publicado automaticamente.

Para gerar/refazer um rascunho manualmente:

```bash
cd pipeline
radar-etl rascunho-contrato --edicao 2026 --raiz ../data
```

---

## Testar a lógica localmente (sem tocar no Azure)

O orquestrador aceita `--sem-deploy`: roda a detecção e a ingestão na sua máquina,
mas não reconstrói nem republica nada.

```bash
./ops/atualizar-dados.sh --sem-deploy
```

Ou só a detecção/ingestão, direto pelo CLI:

```bash
cd pipeline
radar-etl atualizar-auto --raiz ../data
```

Códigos de saída do `atualizar-auto`:

- `0` — nada mudou, ou alguma edição foi processada com sucesso.
- `3` — pelo menos uma edição precisa de contrato novo/ajustado (ação humana).
- `1` — erro real (rede, TLS, ou portão de qualidade reprovado).

---

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED` ao checar/baixar do INEP.** O servidor do INEP manda
uma cadeia TLS incompleta; o projeto complementa com um certificado intermediário em
`pipeline/src/radar_etl/extract/certs/inep_chain.pem`. Esse certificado expira por
volta de 2030. Se a verificação voltar a falhar (expiração ou rotação de CA da RNP),
substitua esse arquivo pelo intermediário vigente — **nunca** desligue a verificação.

**O job diz `reprovada` (portão de qualidade).** Isso é proteção, não bug. Um
portão barra, por exemplo, uma queda abrupta no número de linhas (sinal de arquivo
truncado) ou um schema que não bate com o contrato. Veja a mensagem nos logs do job,
confirme na mão o que o INEP publicou, e só então decida reprocessar.

**O job republica sempre, mesmo sem novidade.** Sinal de que o volume de dados
(`/repo/data`) não está persistindo entre execuções: sem os manifestos antigos, a
detecção acha que tudo é novo. Verifique a montagem do Azure Files no job.
