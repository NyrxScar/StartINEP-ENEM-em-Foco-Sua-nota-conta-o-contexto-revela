"""Atualizacao automatica anual: decide o que (re)ingerir sem intervencao humana.

A premissa desta sprint e que ninguem precise 'lembrar' de rodar a ingestao quando
o INEP publica uma edicao nova ou retifica uma ja publicada. Um agendador roda
`radar-etl atualizar-auto` periodicamente; este modulo responde a unica pergunta
que importa em cada execucao: *para cada ano-alvo, ha algo novo na origem que ainda
nao esta na nossa Prata?* — e, quando ha, delega para o pipeline ja existente.

O recorte de anos e deliberadamente estreito — o ano corrente e o anterior. O ENEM
de um ano e aplicado no fim daquele ano e os microdados saem no ano seguinte (e sao
retificados meses depois), entao em qualquer momento as duas unicas edicoes que
podem *mudar* sao essas duas. Edicoes mais antigas ja estao estabilizadas; re-checa-
las a cada execucao so gastaria requisicoes contra o servidor do INEP sem nunca
encontrar novidade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import httpx

from radar_etl.contracts.modelos import ContratoInvalido, carregar_canonico, carregar_contrato
from radar_etl.extract.fonte import consultar_metadados
from radar_etl.manifesto import Manifesto
from radar_etl.pipeline import Caminhos, EdicaoJaProcessada, executar
from radar_etl.quality.portoes import QualidadeReprovada


class Desfecho(StrEnum):
    """O que aconteceu com um ano-alvo nesta execucao."""

    PROCESSADA = "processada"  # tinha novidade e foi ingerida com sucesso
    SEM_MUDANCA = "sem_mudanca"  # a origem esta igual ao que ja temos: nada a fazer
    CONTRATO_AUSENTE = "contrato_ausente"  # a edicao existe na origem mas nao temos contrato
    FONTE_INDISPONIVEL = "fonte_indisponivel"  # o INEP ainda nao publicou esse ano
    REPROVADA = "reprovada"  # um portao de qualidade barrou a ingestao
    ERRO = "erro"  # falha inesperada (rede, TLS, disco...)


@dataclass(frozen=True)
class ResultadoAno:
    edicao: int
    desfecho: Desfecho
    detalhe: str
    # True quando a Prata mudou de fato nesta execucao. E o sinal que o
    # orquestrador de deploy usa para decidir se vale rebuildar a imagem: sem
    # isso, um agendador que roda toda semana republicaria a mesma imagem sem
    # nenhum dado novo.
    prata_mudou: bool = False


def anos_alvo(referencia: datetime | None = None) -> list[int]:
    """O ano corrente e o anterior — as duas unicas edicoes que ainda podem mudar.

    Usa o ano do calendario em UTC. Em 2026 isto devolve [2026, 2025]; em 2027,
    [2027, 2026]; e assim por diante, sem nenhuma lista fixa para manter.
    """
    ano = (referencia or datetime.now(UTC)).year
    return [ano, ano - 1]


def _fonte_tem_novidade(
    url: str,
    anterior: Manifesto | None,
    cliente: httpx.Client | None = None,
) -> tuple[bool, str]:
    """A origem publica algo diferente do que ja ingerimos?

    Compara os metadados remotos (sem baixar o corpo) com o manifesto da ultima
    ingestao. Uma edicao que nunca ingerimos (`anterior is None`) e sempre novidade.
    Mudanca de tamanho ou de Last-Modified indica publicacao inicial ou retificacao
    — exatamente os dois eventos que queremos capturar.
    """
    meta = consultar_metadados(url, cliente=cliente)

    if anterior is None:
        return True, "primeira ingestao desta edicao"

    if meta.bytes_totais is not None and meta.bytes_totais != anterior.fonte_bytes:
        return True, (
            f"tamanho na origem mudou ({anterior.fonte_bytes} -> {meta.bytes_totais} bytes): "
            "publicacao inicial ou retificacao"
        )

    if (
        meta.last_modified is not None
        and anterior.fonte_last_modified is not None
        and meta.last_modified != anterior.fonte_last_modified
    ):
        return True, (
            f"Last-Modified mudou ('{anterior.fonte_last_modified}' -> "
            f"'{meta.last_modified}'): retificacao provavel"
        )

    return False, "origem identica ao ultimo manifesto"


def atualizar_ano(
    edicao: int,
    caminhos: Caminhos,
    cliente: httpx.Client | None = None,
) -> ResultadoAno:
    """Avalia e, se preciso, (re)ingere uma unica edicao.

    Encapsula todas as formas de 'nada a fazer' e 'precisa de humano' em um
    `ResultadoAno` — nunca levanta por fluxo esperado. So estados realmente
    inesperados viram `Desfecho.ERRO`. Isso deixa o orquestrador decidir o deploy
    a partir de dados, nao de excecoes.
    """
    canonico = carregar_canonico()

    # Sem contrato nao da para saber nem a URL da edicao. Isso NAO e erro: e o
    # ponto exato em que um ano novo (ou uma estrutura de ZIP nova) precisa de um
    # humano para escrever/ajustar o contrato. O orquestrador transforma isto em
    # um rascunho de contrato para revisao.
    try:
        contrato = carregar_contrato(edicao, canonico)
    except ContratoInvalido as erro:
        return ResultadoAno(
            edicao,
            Desfecho.CONTRATO_AUSENTE,
            f"sem contrato valido para {edicao}: {erro}",
        )

    manifesto_path = caminhos.manifests / f"enem_{edicao}.json"
    anterior = Manifesto.carregar(manifesto_path)

    try:
        tem_novidade, motivo = _fonte_tem_novidade(contrato.fonte.url, anterior, cliente)
    except httpx.HTTPStatusError as erro:
        # 404 e o jeito do INEP dizer "essa edicao ainda nao existe". Qualquer
        # outro status e uma falha de verdade que merece atencao.
        if erro.response.status_code == 404:
            return ResultadoAno(
                edicao,
                Desfecho.FONTE_INDISPONIVEL,
                f"o INEP ainda nao publicou {edicao} ({contrato.fonte.url} -> 404)",
            )
        return ResultadoAno(
            edicao, Desfecho.ERRO, f"HTTP {erro.response.status_code} ao checar origem"
        )
    except httpx.HTTPError as erro:
        return ResultadoAno(edicao, Desfecho.ERRO, f"falha de rede ao checar origem: {erro}")

    if not tem_novidade:
        return ResultadoAno(edicao, Desfecho.SEM_MUDANCA, motivo)

    # Ha novidade. `forcar=True` porque ja decidimos aqui, pela origem, que vale
    # reprocessar: a origem mudou (hash/tamanho/Last-Modified) desde o ultimo
    # manifesto. Sem forcar, uma retificacao cujo ZIP ja estivesse em Bronze com o
    # mesmo sha cairia em EdicaoJaProcessada. A verificacao de origem continua
    # ligada: o portao de freshness ainda roda dentro de `executar`.
    #
    # Para capturar uma retificacao, o Bronze antigo precisa sair do caminho —
    # senao `executar` reusa o ZIP ja baixado e nunca ve o arquivo novo.
    zip_path = caminhos.bronze / f"microdados_enem_{edicao}.zip"
    if tem_novidade and anterior is not None and zip_path.exists():
        zip_path.unlink()

    try:
        manifesto = executar(edicao, caminhos, forcar=True, verificar_origem=True)
    except EdicaoJaProcessada as aviso:
        # Caminho raro: a origem sinalizou mudanca mas o conteudo baixado bateu
        # com o ultimo manifesto. Tratamos como sem-mudanca para nao republicar.
        return ResultadoAno(edicao, Desfecho.SEM_MUDANCA, f"reavaliado como inalterado: {aviso}")
    except QualidadeReprovada as erro:
        return ResultadoAno(edicao, Desfecho.REPROVADA, f"portao de qualidade reprovou: {erro}")
    except Exception as erro:  # noqa: BLE001 - o job precisa seguir para o proximo ano
        return ResultadoAno(edicao, Desfecho.ERRO, f"falha inesperada na ingestao: {erro}")

    return ResultadoAno(
        edicao,
        Desfecho.PROCESSADA,
        f"{motivo}; {manifesto.linhas_prata:,} linhas na Prata",
        prata_mudou=True,
    )


def atualizar_automatico(
    caminhos: Caminhos,
    referencia: datetime | None = None,
    cliente: httpx.Client | None = None,
) -> list[ResultadoAno]:
    """Processa o ano corrente e o anterior, devolvendo um resultado por ano.

    Nunca levanta por causa de um ano: um ano que falha nao pode impedir o outro
    de ser avaliado. O chamador (CLI/orquestrador) inspeciona a lista e decide o
    codigo de saida e o deploy.
    """
    return [atualizar_ano(ano, caminhos, cliente) for ano in anos_alvo(referencia)]
