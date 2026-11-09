import zipfile
from pathlib import Path

import typer

app = typer.Typer(help="Pipeline de ingestao dos microdados do ENEM.", no_args_is_help=True)


@app.command()
def ingest(
    edicao: int = typer.Option(..., help="Ano da edicao do ENEM."),
    raiz: Path = typer.Option(Path("../data"), help="Raiz das camadas de dados."),
    forcar: bool = typer.Option(False, "--forcar", help="Reprocessa mesmo sem mudanca na fonte."),
    sem_rede: bool = typer.Option(False, "--sem-rede", help="Pula a verificacao de freshness."),
) -> None:
    """Ingere uma edicao do ENEM: Bronze -> portoes de qualidade -> Prata."""
    from radar_etl.pipeline import Caminhos, EdicaoJaProcessada, executar
    from radar_etl.quality.portoes import QualidadeReprovada

    try:
        manifesto = executar(
            edicao, Caminhos(raiz=raiz), forcar=forcar, verificar_origem=not sem_rede
        )
    except EdicaoJaProcessada as aviso:
        typer.echo(f"[pulado] {aviso}")
        raise typer.Exit(code=0) from aviso
    except QualidadeReprovada as erro:
        typer.echo(f"[REPROVADO] {erro}", err=True)
        raise typer.Exit(code=1) from erro

    reducao = (1 - manifesto.bytes_prata / manifesto.fonte_bytes) * 100
    typer.echo(
        f"[ok] Edicao {manifesto.edicao}: {manifesto.linhas_prata:,} linhas na Prata | "
        f"{manifesto.fonte_bytes / 1_048_576:.1f} MB -> "
        f"{manifesto.bytes_prata / 1_048_576:.1f} MB ({reducao:.1f}% de reducao)"
    )


@app.command()
def inspecionar(
    zip_path: Path = typer.Option(..., "--zip", help="Caminho do ZIP de microdados."),  # noqa: B008
    linhas: int = typer.Option(3, help="Quantas linhas de amostra exibir."),
) -> None:
    """Lista o conteudo do ZIP e o cabecalho do maior CSV, para escrever o contrato da edicao."""
    with zipfile.ZipFile(zip_path) as z:
        infos = sorted(z.infolist(), key=lambda i: i.file_size, reverse=True)

        typer.echo("=== Arquivos no ZIP (maiores primeiro) ===")
        for info in infos:
            typer.echo(f"  {info.file_size / 1_048_576:10.1f} MB  {info.filename}")

        csvs = [i for i in infos if i.filename.lower().endswith(".csv")]
        if not csvs:
            typer.echo("\nNenhum CSV encontrado no ZIP.")
            raise typer.Exit(code=1)

        maior = csvs[0]
        typer.echo(f"\n=== Cabecalho de {maior.filename} ===")
        with z.open(maior.filename) as f:
            bruto = f.read(256 * 1024)

        for codificacao in ("utf-8", "latin-1"):
            try:
                texto = bruto.decode(codificacao)
            except UnicodeDecodeError:
                typer.echo(f"  encoding {codificacao}: FALHOU")
                continue
            typer.echo(f"  encoding {codificacao}: OK")
            for numero, linha in enumerate(texto.splitlines()[: linhas + 1]):
                rotulo = "cabecalho" if numero == 0 else f"linha {numero}"
                typer.echo(f"    [{rotulo}] {linha[:400]}")
            break


@app.command("atualizar-auto")
def atualizar_auto(
    raiz: Path = typer.Option(Path("../data"), help="Raiz das camadas de dados."),  # noqa: B008
    ano: list[int] = typer.Option(  # noqa: B008
        None,
        "--ano",
        help="Forca um ano especifico (repetivel). Sem isso, usa o ano corrente e o anterior.",
    ),
) -> None:
    """Checa o INEP e ingere so o que mudou, para o ano corrente e o anterior.

    Projetado para rodar sem humano (Azure Container Apps Job agendado). Nunca
    aborta por causa de um ano: avalia todos e resume ao final. O codigo de saida
    comunica ao orquestrador o que fazer a seguir:

      0  nada mudou OU alguma edicao foi processada com sucesso (ver 'prata_mudou')
      3  pelo menos uma edicao precisa de um contrato novo/ajustado (acao humana)
      1  erro de verdade (rede/TLS/qualidade) em pelo menos uma edicao

    A linha 'PRATA_MUDOU=sim|nao' no stdout e o contrato com o script de deploy:
    ele so rebuilda a imagem quando houve mudanca real na Prata.
    """
    from radar_etl.auto import Desfecho, anos_alvo, atualizar_ano
    from radar_etl.pipeline import Caminhos

    caminhos = Caminhos(raiz=raiz)
    alvos = ano or anos_alvo()

    resultados = [atualizar_ano(e, caminhos) for e in alvos]

    typer.echo("=== Atualizacao automatica ENEM ===")
    for r in resultados:
        typer.echo(f"  [{r.edicao}] {r.desfecho.value}: {r.detalhe}")

    prata_mudou = any(r.prata_mudou for r in resultados)
    typer.echo(f"PRATA_MUDOU={'sim' if prata_mudou else 'nao'}")

    precisa_contrato = any(r.desfecho is Desfecho.CONTRATO_AUSENTE for r in resultados)
    houve_erro = any(r.desfecho in (Desfecho.ERRO, Desfecho.REPROVADA) for r in resultados)

    if houve_erro:
        falhos = (Desfecho.ERRO, Desfecho.REPROVADA)
        anos_erro = [r.edicao for r in resultados if r.desfecho in falhos]
        typer.echo(f"[ERRO] Falha em: {anos_erro}", err=True)
        raise typer.Exit(code=1)

    if precisa_contrato:
        anos_sem = [r.edicao for r in resultados if r.desfecho is Desfecho.CONTRATO_AUSENTE]
        typer.echo(
            f"[ACAO HUMANA] Edicao(oes) {anos_sem} na origem sem contrato valido. "
            f"Rode 'radar-etl rascunho-contrato --edicao <ANO>' e revise o YAML.",
            err=True,
        )
        raise typer.Exit(code=3)

    raise typer.Exit(code=0)


@app.command("rascunho-contrato")
def rascunho_contrato(
    edicao: int = typer.Option(..., help="Ano da edicao sem contrato ou com estrutura nova."),
    raiz: Path = typer.Option(Path("../data"), help="Raiz das camadas de dados."),  # noqa: B008
) -> None:
    """Baixa o ZIP da edicao e gera um contrato-RASCUNHO para revisao humana.

    Use quando 'atualizar-auto' sinaliza CONTRATO_AUSENTE: o INEP publicou um ano
    (ou mudou a estrutura do ZIP) e nao ha como ingerir sem um contrato. Este comando
    inspeciona o ZIP, casa as colunas conhecidas, e escreve enem_<ANO>.rascunho.yml.
    O rascunho NAO e usado como contrato ate alguem revisa-lo e renomea-lo para
    enem_<ANO>.yml.
    """
    from radar_etl.extract.fonte import baixar
    from radar_etl.pipeline import Caminhos
    from radar_etl.rascunho import RascunhoImpossivel, salvar_rascunho

    caminhos = Caminhos(raiz=raiz)
    zip_path = caminhos.bronze / f"microdados_enem_{edicao}.zip"
    url = f"https://download.inep.gov.br/microdados/microdados_enem_{edicao}.zip"

    if not zip_path.exists():
        typer.echo(f"Baixando {url} ...")
        try:
            baixar(url, zip_path)
        except Exception as erro:  # noqa: BLE001
            typer.echo(f"[ERRO] Nao consegui baixar o ZIP de {edicao}: {erro}", err=True)
            raise typer.Exit(code=1) from erro

    try:
        destino = salvar_rascunho(edicao, zip_path)
    except RascunhoImpossivel as erro:
        typer.echo(f"[ERRO] {erro}", err=True)
        raise typer.Exit(code=1) from erro

    typer.echo(f"[ok] Rascunho escrito em {destino}")
    typer.echo(
        "Revise o arquivo, ajuste as entradas marcadas com REVISAR, e renomeie para "
        f"enem_{edicao}.yml para ativar o contrato."
    )
