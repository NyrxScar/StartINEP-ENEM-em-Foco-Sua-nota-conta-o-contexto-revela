"""Gera um RASCUNHO de contrato de edicao a partir do ZIP que o INEP publicou.

Este e o passo que, de forma honesta, nao da para 100% automatizar: o INEP muda
o nome do arquivo CSV dentro do ZIP, o conjunto de colunas e ate *quais dados*
existem de uma edicao para a outra (2024 so tem RESULTADOS, 2025 so tem
PARTICIPANTES sem notas). Decidir o significado disso — qual arquivo ingerir, se
uma coluna ausente e uma lacuna legitima ou um erro — exige julgamento humano, e
os portoes de qualidade do pipeline existem justamente para nao deixar essa decisao
passar em silencio.

O que DA para automatizar e tirar 90% do trabalho mecanico: abrir o ZIP, achar o
CSV de dados, ler o cabecalho, e casar cada coluna de origem conhecida com o nome
canonico correspondente. O resultado e um YAML de rascunho — com um bloco de REVISAR
no topo — que um humano confere em minutos em vez de escrever do zero.

O rascunho NUNCA e tratado como contrato valido automaticamente: ele e escrito com
sufixo `.rascunho.yml`, fora do que `carregar_contrato` procura (`enem_<ANO>.yml`),
de proposito. So vira contrato quando alguem revisa e renomeia.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from radar_etl.contracts.modelos import DIRETORIO_PADRAO, carregar_canonico

# Mapa canonico <- coluna de origem do INEP. E o inverso do que um contrato declara
# em `mapeamento`, consolidando os nomes de origem vistos nas edicoes 2020-2025.
# Varias colunas canonicas tem mais de um nome de origem possivel porque o INEP
# renomeou colunas ao longo dos anos (ex.: a renda e Q006 em uns anos, com outro
# codigo em outros); listamos os candidatos e casamos o primeiro presente.
ORIGENS_CONHECIDAS: dict[str, list[str]] = {
    "ano": ["NU_ANO"],
    "uf_prova": ["SG_UF_PROVA"],
    "municipio_prova": ["NO_MUNICIPIO_PROVA"],
    "codigo_escola": ["CO_ESCOLA"],
    "localizacao_escola": ["TP_LOCALIZACAO_ESC"],
    "tipo_escola": ["TP_ESCOLA"],
    "dependencia_adm_escola": ["TP_DEPENDENCIA_ADM_ESC"],
    "faixa_etaria": ["TP_FAIXA_ETARIA"],
    "sexo": ["TP_SEXO"],
    "cor_raca": ["TP_COR_RACA"],
    "treineiro": ["IN_TREINEIRO"],
    "renda_familiar": ["Q006"],
    "escolaridade_pai": ["Q001"],
    "escolaridade_mae": ["Q002"],
    "presenca_cn": ["TP_PRESENCA_CN"],
    "presenca_ch": ["TP_PRESENCA_CH"],
    "presenca_lc": ["TP_PRESENCA_LC"],
    "presenca_mt": ["TP_PRESENCA_MT"],
    "nota_cn": ["NU_NOTA_CN"],
    "nota_ch": ["NU_NOTA_CH"],
    "nota_lc": ["NU_NOTA_LC"],
    "nota_mt": ["NU_NOTA_MT"],
    "nota_redacao": ["NU_NOTA_REDACAO"],
}

# Colunas canonicas que nunca vem de uma coluna de origem: sao derivadas no SQL.
# Ficam sempre no bloco `derivadas` do rascunho, nunca em `mapeamento`/`ausentes`.
DERIVADAS_FIXAS: dict[str, str] = {"regiao": "regiao_por_uf"}


class RascunhoImpossivel(Exception):
    """Nao da para inferir um rascunho (ZIP sem CSV, cabecalho ilegivel)."""


def _ler_cabecalho(zip_path: Path, nome_csv: str) -> list[str]:
    """Le so o cabecalho do CSV sem extrair o arquivo inteiro.

    Tenta utf-8 e latin-1 (as duas codificacoes que o INEP usa). O separador e
    sempre ';' nos microdados do ENEM.
    """
    with zipfile.ZipFile(zip_path) as z, z.open(nome_csv) as bruto:
        primeira = io.BufferedReader(bruto).readline()
    for codificacao in ("utf-8", "latin-1"):
        try:
            texto = primeira.decode(codificacao)
        except UnicodeDecodeError:
            continue
        return [c.strip() for c in texto.rstrip("\r\n").split(";")]
    raise RascunhoImpossivel(f"Nao consegui decodificar o cabecalho de {nome_csv} (utf-8/latin-1).")


def _escolher_csv(zip_path: Path) -> zipfile.ZipInfo:
    """Escolhe o CSV de dados: o maior CSV do ZIP.

    Nos microdados do ENEM o arquivo de participantes/resultados e sempre o maior
    (milhoes de linhas), muito acima de dicionarios e itens de prova. O humano
    confirma essa escolha ao revisar o rascunho — por isso ela aparece comentada
    no topo do YAML.
    """
    with zipfile.ZipFile(zip_path) as z:
        csvs = sorted(
            (i for i in z.infolist() if i.filename.lower().endswith(".csv")),
            key=lambda i: i.file_size,
            reverse=True,
        )
    if not csvs:
        raise RascunhoImpossivel(f"Nenhum CSV encontrado em {zip_path.name}.")
    return csvs[0]


def _bloco_yaml_mapeamento(mapeadas: dict[str, str]) -> str:
    linhas = ["mapeamento:"]
    for canonico, origem in mapeadas.items():
        linhas.append(f"  {canonico}: {origem}")
    return "\n".join(linhas)


def _bloco_yaml_derivadas() -> str:
    linhas = ["derivadas:"]
    for canonico, fn in DERIVADAS_FIXAS.items():
        linhas.append(f"  {canonico}: {fn}")
    return "\n".join(linhas)


def _bloco_yaml_ausentes(ausentes: list[str]) -> str:
    if not ausentes:
        return "ausentes: {}"
    linhas = ["ausentes:"]
    for canonico in ausentes:
        linhas.append(
            f"  {canonico}: >-\n"
            f"    REVISAR: nenhuma coluna de origem conhecida para '{canonico}' foi\n"
            f"    encontrada no cabecalho deste CSV. Confirme se a coluna realmente\n"
            f"    nao existe nesta edicao e troque este texto por uma justificativa\n"
            f"    real (ou mova '{canonico}' para 'mapeamento' se houver uma coluna\n"
            f"    de origem equivalente com outro nome)."
        )
    return "\n".join(linhas)


def gerar_rascunho(edicao: int, zip_path: Path) -> str:
    """Produz o texto YAML de um contrato-rascunho para a edicao.

    Casa colunas por nome conhecido; o que nao casa vai para `ausentes` com um
    placeholder REVISAR. O objetivo e um ponto de partida revisavel, nao um contrato
    pronto: so o portao de schema (no `ingest`) confirma que o mapeamento fecha.
    """
    canonico = carregar_canonico()
    info = _escolher_csv(zip_path)
    cabecalho = _ler_cabecalho(zip_path, info.filename)
    presentes = set(cabecalho)

    mapeadas: dict[str, str] = {}
    ausentes: list[str] = []
    for col in canonico.colunas:
        if col in DERIVADAS_FIXAS:
            continue
        origem = next((o for o in ORIGENS_CONHECIDAS.get(col, []) if o in presentes), None)
        if origem is not None:
            mapeadas[col] = origem
        else:
            ausentes.append(col)

    nao_casadas = sorted(presentes - set(mapeadas.values()))

    cabecalho_yaml = (
        f"# RASCUNHO GERADO AUTOMATICAMENTE — REVISAR ANTES DE USAR.\n"
        f"#\n"
        f"# Este arquivo NAO e um contrato valido ate ser revisado e renomeado para\n"
        f"# enem_{edicao}.yml. Um humano precisa:\n"
        f"#   1. Confirmar que '{info.filename}' e o CSV certo a ingerir\n"
        f"#      (escolhido por ser o maior CSV do ZIP, {info.file_size / 1_048_576:.1f} MB).\n"
        f"#   2. Revisar cada entrada em 'ausentes' marcada com REVISAR e escrever uma\n"
        f"#      justificativa real (os portoes exigem justificativa nao-vazia).\n"
        f"#   3. Conferir o encoding (chutado como latin-1, o padrao do INEP).\n"
        f"#\n"
        f"# Colunas do CSV que NAO casaram com nenhum nome canonico conhecido\n"
        f"# (podem ser colunas novas do INEP a ignorar, ou renomeacoes a mapear):\n"
        f"#   {', '.join(nao_casadas) if nao_casadas else '(nenhuma)'}\n"
    )

    corpo = (
        f"edicao: {edicao}\n"
        f"versao_contrato: 1\n"
        f"\n"
        f"fonte:\n"
        f'  url: "https://download.inep.gov.br/microdados/microdados_enem_{edicao}.zip"\n'
        f'  arquivo_csv: "{info.filename}"\n'
        f'  separador: ";"\n'
        f'  encoding: "latin-1"\n'
        f"\n"
        f"{_bloco_yaml_mapeamento(mapeadas)}\n"
        f"\n"
        f"{_bloco_yaml_derivadas()}\n"
        f"\n"
        f"{_bloco_yaml_ausentes(ausentes)}\n"
    )

    return cabecalho_yaml + "\n" + corpo


def salvar_rascunho(edicao: int, zip_path: Path, diretorio: Path | None = None) -> Path:
    """Escreve o rascunho em enem_<ANO>.rascunho.yml. Nunca sobrescreve um contrato real."""
    diretorio = diretorio or DIRETORIO_PADRAO
    contrato_real = diretorio / f"enem_{edicao}.yml"
    if contrato_real.exists():
        raise RascunhoImpossivel(
            f"Ja existe um contrato real em {contrato_real.name}. "
            f"Para refaze-lo, remova-o antes (ou edite-o a mao)."
        )
    destino = diretorio / f"enem_{edicao}.rascunho.yml"
    destino.write_text(gerar_rascunho(edicao, zip_path), encoding="utf-8")
    return destino
