"""gerar_relatorio: HTML autocontido com gráficos SVG."""
import json

import pytest

pytestmark = pytest.mark.anyio

SECOES = ("Resumo", "Alertas", "Voo", "Estabilidade e massa", "Segurança e estrutura", "Condições de lançamento")


async def test_relatorio_basico(chamar, workspace):
    erro, d = await chamar("gerar_relatorio", arquivo_ork="foguete.ork", arquivo_html="rel/basico.html",
                           titulo="Teste <script>alert(1)</script>")
    assert not erro, d
    assert d["arquivo_html"] == "rel/basico.html" and d["resumo"]["apogeu_m"] > 0
    html = (workspace / "rel" / "basico.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and html.count("<svg") == 4  # trajetória, subida, empuxo, pouso
    assert all(f"<h2>{s}</h2>" in html for s in SECOES)
    assert "<script>" not in html and "&lt;script&gt;" in html  # texto do usuário escapado
    assert "<h2>Motor (" not in html and "<h2>Dispersão" not in html  # seções opcionais ausentes
    erro, msg = await chamar("gerar_relatorio", arquivo_ork="foguete.ork", arquivo_html="rel/basico.html")
    assert erro and "já existe" in msg


async def test_relatorio_com_motor_e_dispersao(chamar, workspace):
    progresso = []

    async def cb(p, total, msg):
        progresso.append(p)

    res = await chamar.cliente.call_tool("gerar_relatorio", {
        "arquivo_ork": "foguete.ork", "arquivo_html": "rel/completo.html", "arquivo_ric": "motor.ric",
        "material_aletas": "aluminio", "dispersao": {"n_voos": 5, "semente": 2}}, progress_callback=cb)
    assert not res.is_error, res.content[0].text
    d = json.loads(res.content[0].text)
    assert d["dispersao"]["n_voos"] == 5 and progresso == [1, 2, 3, 4, 5]
    html = (workspace / "rel" / "completo.html").read_text(encoding="utf-8")
    assert "<h2>Motor (motor.ric)</h2>" in html and "<h2>Dispersão (5 voos, semente 2)</h2>" in html
    assert html.count("<svg") == 5  # 3 do voo + motor + pousos com elipse
    assert "Flutter" in html and "elipse de 95%" in html


@pytest.mark.parametrize("kw, trecho", [
    ({"arquivo_html": "rel/x.txt"}, "extensão .html"),
    ({"dispersao": {"voos": 5}}, "n_voos, incertezas e/ou semente"),
    ({"dispersao": {"n_voos": 2}}, "de 5 a 200"),
    ({"arquivo_ric": "nao_existe.ric"}, "não encontrado"),
])
async def test_relatorio_invalido(chamar, kw, trecho):
    args = {"arquivo_ork": "foguete.ork", "arquivo_html": "rel/invalido.html"} | kw
    erro, msg = await chamar("gerar_relatorio", **args)
    assert erro and trecho in msg
