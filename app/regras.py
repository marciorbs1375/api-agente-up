"""Travas de negócio, funções puras. O agente não passa por cima delas."""
from dataclasses import dataclass


class ErroNegocio(Exception):
    def __init__(self, codigo: str, mensagem: str):
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


@dataclass
class Item:
    codigo: int
    quantidade: float


def validar_itens(itens: list[Item], max_itens: int, qtd_max: float) -> None:
    if not itens:
        raise ErroNegocio("ITENS_INVALIDOS", "Nenhum item informado.")
    if len(itens) > max_itens:
        raise ErroNegocio("LIMITE_PEDIDO_EXCEDIDO", f"No máximo {max_itens} itens por pedido neste momento.")
    vistos = set()
    for i in itens:
        if i.codigo in vistos:
            raise ErroNegocio("ITENS_INVALIDOS", "Item repetido. Some as quantidades em uma linha só.")
        vistos.add(i.codigo)
        if i.quantidade <= 0 or i.quantidade > qtd_max:
            raise ErroNegocio("ITENS_INVALIDOS", f"Quantidade inválida para o produto {i.codigo}.")


def validar_total(total: float, valor_max: float) -> None:
    if total > valor_max:
        raise ErroNegocio("LIMITE_PEDIDO_EXCEDIDO",
                          "Esse valor passa do limite que posso fechar por aqui. Vou registrar para a equipe da UP.")


def validar_cliente(cliente: dict) -> None:
    if not cliente.get("ativo"):
        raise ErroNegocio("CREDITO_BLOQUEADO", "Cadastro inativo. Não consigo fechar a venda por aqui.")


def validar_estoque(codigo: int, pedido: float, disponivel: float | None) -> None:
    if disponivel is None or disponivel < pedido:
        raise ErroNegocio("ESTOQUE_INSUFICIENTE",
                          f"Não consegui confirmar estoque suficiente do produto {codigo}.")


def conferir_precos(orcado: dict[int, float], atual: dict[int, float]) -> None:
    """Preço do pedido tem de ser igual ao do orçamento. Qualquer diferença bloqueia."""
    for codigo, preco in orcado.items():
        if codigo not in atual or round(atual[codigo], 2) != round(preco, 2):
            raise ErroNegocio("PRECO_DIVERGENTE", "O preço mudou desde o orçamento. Preciso refazer o orçamento.")
